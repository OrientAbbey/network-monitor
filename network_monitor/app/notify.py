"""
Alertes sortantes : Telegram (groupe), webhook signé, SMS (groupe).

Fonctionnement — un « outbox » transactionnel :
1. À chaque tick, `plan()` transforme les alertes confirmées en envois (une ligne par alerte et
   par canal), écrits **dans la même transaction que l'alerte** : un envoi ne peut ni précéder ni
   survivre à l'alerte qui l'a déclenché, et un redémarrage ne perd rien.
2. Un thread (`NotificationDispatcher`) relit les envois dus, **regroupe** ceux d'un canal arrivés
   à moins de `batch_window` l'un de l'autre en un seul message, respecte le plafond de messages
   du canal, envoie, puis marque chaque ligne.
3. Un échec temporaire (réseau, 5xx, 429) est retenté avec un délai croissant ; un refus définitif
   (jeton invalide, destinataire inexistant) n'est jamais retenté. Pour un SMS de groupe, seuls les
   destinataires qui ont échoué sont retentés : les autres ne reçoivent pas deux fois le message.

Garantie : au moins une fois (une coupure entre l'envoi et son enregistrement peut doubler un
message). Le webhook porte un identifiant de livraison stable pour que le récepteur déduplique.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from network_monitor.app.config import (
    HeartbeatConfig,
    NotificationsConfig,
    SmsChannel,
    TelegramChannel,
    WebhookChannel,
)
from network_monitor.incidents.manager import Alert
from network_monitor.storage.repository import Notification, Repository

logger = logging.getLogger(__name__)

# httpx journalise chaque requête à INFO avec son URL complète. Or ici l'URL est un secret (jeton Telegram, URL du
# signal de vie) : un simple `logging.basicConfig(level=INFO)` côté exploitation suffirait à la divulguer.
logging.getLogger("httpx").setLevel(logging.WARNING)

_MAX_BACKOFF = timedelta(minutes=15)

_SEVERITY_ICON = {"info": "🔵", "warning": "🟠", "critical": "🔴"}

# Catalogues de messages. Le français est le texte d'origine, inchangé ; chaque langue fournit les mêmes clés. Les gabarits d'un
# catalogue sont internes (ils peuvent porter des formats numériques) ; ceux de l'utilisateur (`templates`) n'acceptent que des champs.
_CATALOG: dict[str, dict[str, Any]] = {
    "fr": {
        "types": {
            "traffic_drop": "chute de trafic", "traffic_spike": "pic de trafic", "high_loss": "pertes élevées",
            "traffic_drop_with_high_loss": "chute de trafic avec pertes", "congestion": "congestion",
            "progressive_degradation": "dégradation progressive", "intermittent_degradation": "dégradation intermittente",
        },
        "severity": {"info": "info", "warning": "avertissement", "critical": "critique"},
        "verb": {"opened": "ouvert", "updated": "mis à jour", "reopened": "rouvert", "closed": "résolu", "stale": "données manquantes", "escalated": "ESCALADE"},
        "head": {"opened": "INCIDENT", "reopened": "INCIDENT ROUVERT", "updated": "MISE À JOUR"},
        "resolved": "🟢 RÉSOLU — {output} : {type}",
        "resolved_detail": "Durée : {duration} ({start} → {end})",
        "escalated": "🔺 ESCALADE — {output} : {type} ({severity})",
        "escalated_detail": "Aucun accusé de réception : incident en cours depuis {duration} (début {start})",
        "stale": "⚪ DONNÉES MANQUANTES — {output}",
        "stale_detail": "L'incident en cours ne peut plus être suivi : plus de mesures reçues.",
        "open": "{icon} {head} — {output} : {type} ({severity})",
        "open_detail": "Début : {start} (confirmé {at})",
        "traffic_line": "Trafic : {traffic:.0f} Mbps (attendu {expected:.0f}{deviation}",
        "loss_line": "Pertes : {loss:.2f} %",
        "also": "Explique aussi : {outputs}",
        "signed_pct": "{value:+.0f} %",
        "many": "{n} alertes",
        "more": "… et {n} autres",
        "sms_resolved": "RESOLU {output} {type}, duree {duration}",
        "sms_crit": "CRIT", "sms_alert": "ALERTE",
        "sms_escalated": "ESCALADE {sev} {output} {type} depuis {start}, sans accuse de reception",
        "sms_open": "{sev} {output} {type} depuis {start}",
        "sms_links": " +{n} liens",
        "sms_many": "NM: {n} alertes ({opened} en cours, {closed} resolues) {outputs}",
    },
    "en": {
        "types": {
            "traffic_drop": "traffic drop", "traffic_spike": "traffic spike", "high_loss": "high packet loss",
            "traffic_drop_with_high_loss": "traffic drop with packet loss", "congestion": "congestion",
            "progressive_degradation": "progressive degradation", "intermittent_degradation": "intermittent degradation",
        },
        "severity": {"info": "info", "warning": "warning", "critical": "critical"},
        "verb": {"opened": "open", "updated": "updated", "reopened": "reopened", "closed": "resolved", "stale": "data missing", "escalated": "ESCALATION"},
        "head": {"opened": "INCIDENT", "reopened": "INCIDENT REOPENED", "updated": "UPDATE"},
        "resolved": "🟢 RESOLVED — {output}: {type}",
        "resolved_detail": "Duration: {duration} ({start} → {end})",
        "escalated": "🔺 ESCALATION — {output}: {type} ({severity})",
        "escalated_detail": "Not acknowledged: incident ongoing for {duration} (started {start})",
        "stale": "⚪ DATA MISSING — {output}",
        "stale_detail": "The ongoing incident can no longer be followed: no measurements received.",
        "open": "{icon} {head} — {output}: {type} ({severity})",
        "open_detail": "Started: {start} (confirmed {at})",
        "traffic_line": "Traffic: {traffic:.0f} Mbps (expected {expected:.0f}{deviation}",
        "loss_line": "Packet loss: {loss:.2f}%",
        "also": "Also explains: {outputs}",
        "signed_pct": "{value:+.0f}%",
        "many": "{n} alerts",
        "more": "… and {n} more",
        "sms_resolved": "RESOLVED {output} {type}, lasted {duration}",
        "sms_crit": "CRIT", "sms_alert": "ALERT",
        "sms_escalated": "ESCALATION {sev} {output} {type} since {start}, not acknowledged",
        "sms_open": "{sev} {output} {type} since {start}",
        "sms_links": " +{n} links",
        "sms_many": "NM: {n} alerts ({opened} ongoing, {closed} resolved) {outputs}",
    },
}


# --- Sérialisation et mise en forme ---


def alert_to_dict(alert: Alert) -> dict[str, Any]:
    raw = asdict(alert)
    for key, value in raw.items():
        if isinstance(value, datetime):
            raw[key] = value.isoformat()
        elif hasattr(value, "value"):  # Enum
            raw[key] = value.value
    return raw


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _hm(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%H:%M") if value else "?"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if rest else f"{hours} h"


def _label(alert: dict[str, Any], cat: dict[str, Any]) -> str:
    return cat["types"].get(alert["incident_type"], alert["incident_type"])


def _catalog(lang: str) -> dict[str, Any]:
    return _CATALOG.get(lang, _CATALOG["fr"])


def _fields(a: dict[str, Any], tz: ZoneInfo, cat: dict[str, Any]) -> dict[str, str]:
    """Les valeurs qu'un modèle de l'utilisateur peut insérer (`TEMPLATE_FIELDS`), déjà mises en forme ; « ? » quand elles manquent."""
    started = _dt(a.get("started_at")) or _dt(a["timestamp"])
    ended = _dt(a.get("ended_at")) or _dt(a["timestamp"])
    dev = a.get("traffic_deviation_percent")
    return {
        "output": a["output_id"], "event": cat["verb"].get(a["event"], a["event"]), "type": _label(a, cat),
        "severity": cat["severity"].get(a["severity"], a["severity"]), "start": _hm(started, tz), "end": _hm(ended, tz),
        "at": _hm(_dt(a["timestamp"]), tz), "duration": _duration(a.get("duration_seconds")),
        "traffic": f"{a['traffic_mbps']:.0f} Mbps" if a.get("traffic_mbps") is not None else "?",
        "expected": f"{a['expected_traffic_mbps']:.0f} Mbps" if a.get("expected_traffic_mbps") is not None else "?",
        "deviation": cat["signed_pct"].format(value=dev) if dev is not None else "?",
        "loss": f"{a['loss_percent']:.2f} %" if a.get("loss_percent") is not None else "?",
        "impacted": ", ".join(a.get("impacted_outputs") or []), "message": a.get("message", ""),
    }


def _custom(a: dict[str, Any], tz: ZoneInfo, cat: dict[str, Any], templates: Mapping[str, str] | None) -> str | None:
    """Le modèle de l'utilisateur pour cet événement, rendu, ou None s'il n'en a pas défini (le message intégré s'applique)."""
    template = (templates or {}).get(a["event"])
    return template.format(**_fields(a, tz, cat)) if template is not None else None


def _line(alert: dict[str, Any], tz: ZoneInfo, cat: dict[str, Any]) -> str:
    icon = "🟢" if alert["event"] == "closed" else _SEVERITY_ICON.get(alert["severity"], "")
    return f"{_hm(_dt(alert['timestamp']), tz)} {icon} {alert['output_id']} {cat['verb'].get(alert['event'], alert['event'])} — {_label(alert, cat)}"


def format_text(alerts: list[dict[str, Any]], tz: ZoneInfo, limit: int = 4000, lang: str = "fr", templates: Mapping[str, str] | None = None) -> str:
    """Message lisible sans connaître le fonctionnement du modèle : quoi, où, depuis quand, combien.
    `lang` choisit le catalogue ; `templates` (événement → modèle) remplace le message intégré des événements qu'il nomme."""
    cat = _catalog(lang)
    if len(alerts) == 1:
        a = alerts[0]
        custom = _custom(a, tz, cat, templates)
        if custom is not None:
            return custom[:limit]
        f = _fields(a, tz, cat)
        if a["event"] == "closed":
            lines = [cat["resolved"].format(**f), cat["resolved_detail"].format(**f)]
        elif a["event"] == "escalated":
            lines = [cat["escalated"].format(**f), cat["escalated_detail"].format(**f)]
        elif a["event"] == "stale":
            lines = [cat["stale"].format(**f), cat["stale_detail"]]
        else:
            head = cat["head"].get(a["event"], a["event"].upper())
            lines = [cat["open"].format(icon=_SEVERITY_ICON.get(a["severity"], ""), head=head, **f), cat["open_detail"].format(**f)]
            if a.get("traffic_mbps") is not None and a.get("expected_traffic_mbps"):
                dev = a.get("traffic_deviation_percent")
                closing = ", " + cat["signed_pct"].format(value=dev) + ")" if dev is not None else ")"
                lines.append(cat["traffic_line"].format(traffic=a["traffic_mbps"], expected=a["expected_traffic_mbps"], deviation=closing))
            if a.get("loss_percent") is not None:
                lines.append(cat["loss_line"].format(loss=a["loss_percent"]))
            if a.get("impacted_outputs"):
                lines.append(cat["also"].format(outputs=", ".join(a["impacted_outputs"])))
        return "\n".join(lines)[:limit]
    shown = [_custom(a, tz, cat, templates) or _line(a, tz, cat) for a in alerts[:12]]
    if len(alerts) > 12:
        shown.append(cat["more"].format(n=len(alerts) - 12))
    return (cat["many"].format(n=len(alerts)) + "\n" + "\n".join(shown))[:limit]


def format_sms(alerts: list[dict[str, Any]], tz: ZoneInfo, lang: str = "fr", templates: Mapping[str, str] | None = None) -> str:
    """Un seul segment SMS (≤ 160 caractères, alphabet GSM : sans accents ni émojis) : un message
    plus long ou accentué est facturé en plusieurs segments. Un modèle de l'utilisateur est lui aussi dépouillé de ses accents."""
    cat = _catalog(lang)

    def ascii_(text: str) -> str:
        import unicodedata

        return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()

    def one(a: dict[str, Any]) -> str:
        custom = _custom(a, tz, cat, templates)
        if custom is not None:
            return ascii_(custom)
        f = _fields(a, tz, cat)
        f["type"] = ascii_(f["type"])
        if a["event"] == "closed":
            return cat["sms_resolved"].format(**f)
        sev = cat["sms_crit"] if a["severity"] == "critical" else cat["sms_alert"]
        if a["event"] == "escalated":
            return cat["sms_escalated"].format(sev=sev, **f)
        dev = a.get("traffic_deviation_percent")
        extra = cat["sms_links"].format(n=len(a["impacted_outputs"])) if a.get("impacted_outputs") else ""
        return cat["sms_open"].format(sev=sev, **f) + (f" ({dev:+.0f}%)" if dev is not None else "") + extra

    if len(alerts) == 1:
        return one(alerts[0])[:160]
    opened = sum(a["event"] != "closed" for a in alerts)
    outputs = ",".join(dict.fromkeys(a["output_id"] for a in alerts))
    return cat["sms_many"].format(n=len(alerts), opened=opened, closed=len(alerts) - opened, outputs=outputs)[:160]


# --- Canaux ---


class DeliveryError(Exception):
    """Échec d'envoi ; `retryable` dit si cela vaut la peine de réessayer."""

    def __init__(self, message: str, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _http_error(response: httpx.Response, what: str) -> DeliveryError:
    retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
    retry_after = None
    try:
        retry_after = float(response.headers.get("retry-after") or response.json().get("parameters", {}).get("retry_after"))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        pass
    return DeliveryError(f"{what} : HTTP {response.status_code} {response.text[:200]}", retryable, retry_after)


class Channel:
    """Un canal d'envoi. `targets()` liste les destinataires indépendants (un SMS par numéro) ;
    `send()` renvoie, pour chacun, None (livré) ou l'erreur."""

    def __init__(self, cfg: Any, tz: ZoneInfo, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name: str = cfg.name
        self._tz = tz
        self._client = client

    def targets(self) -> list[str]:
        return ["*"]

    def accepts(self, alert: Alert) -> bool:
        return (
            alert.confirmed
            and alert.event in self.cfg.events
            and alert.severity.rank >= self.cfg.min_severity.rank
        )

    def send(self, alerts: list[dict[str, Any]], targets: list[str]) -> dict[str, DeliveryError | None]:
        raise NotImplementedError

    def _call(self, fn) -> DeliveryError | None:
        try:
            return fn()
        except httpx.TransportError as exc:  # réseau, délai, DNS : toujours temporaire
            return DeliveryError(f"{type(exc).__name__}: {exc}", retryable=True)


class TelegramNotifier(Channel):
    def __init__(self, cfg: TelegramChannel, tz: ZoneInfo, client: httpx.Client) -> None:
        super().__init__(cfg, tz, client)
        self._token = os.environ.get(cfg.token_env)
        if not self._token:
            raise RuntimeError(f"canal {cfg.name} : variable d'environnement {cfg.token_env} absente")

    def send(self, alerts, targets):
        def go() -> DeliveryError | None:
            res = self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self.cfg.chat_id, "text": format_text(alerts, self._tz, lang=self.cfg.language, templates=self.cfg.templates), "disable_web_page_preview": True},
            )
            if res.status_code == 200 and res.json().get("ok"):
                return None
            err = _http_error(res, "Telegram")
            # Un jeton ne doit jamais fuiter dans les journaux ni en base.
            err.args = (str(err).replace(self._token or "", "***"),)
            return err

        return {"*": self._call(go)}


class WebhookNotifier(Channel):
    def __init__(self, cfg: WebhookChannel, tz: ZoneInfo, client: httpx.Client) -> None:
        super().__init__(cfg, tz, client)
        self._url = os.environ.get(cfg.url_env)
        if not self._url or not self._url.startswith(("http://", "https://")):
            raise RuntimeError(f"canal {cfg.name} : variable {cfg.url_env} absente ou n'est pas une URL http(s)")
        self._secret = os.environ.get(cfg.secret_env) if cfg.secret_env else None

    def send(self, alerts, targets):
        sent_at = datetime.now(timezone.utc).isoformat()
        body = json.dumps({"version": 1, "sent_at": sent_at, "alerts": alerts, "text": format_text(alerts, self._tz, lang=self.cfg.language, templates=self.cfg.templates)},
                          ensure_ascii=False, separators=(",", ":")).encode()
        # Identifiant stable : le même lot rejoué (nouvelle tentative) garde le même identifiant.
        delivery = hashlib.sha256("|".join(sorted(a["incident_id"] + a["event"] + a["timestamp"] for a in alerts)).encode()).hexdigest()[:32]
        headers = {"Content-Type": "application/json", "X-NM-Delivery": delivery, "X-NM-Timestamp": sent_at}
        if self._secret:
            mac = hmac.new(self._secret.encode(), sent_at.encode() + b"." + body, hashlib.sha256).hexdigest()
            headers["X-NM-Signature"] = f"sha256={mac}"

        def go() -> DeliveryError | None:
            res = self._client.post(self._url or "", content=body, headers=headers)
            return None if res.is_success else _http_error(res, "Webhook")

        return {"*": self._call(go)}


class SmsNotifier(Channel):
    _ORANGE_TOKEN_URL = "https://api.orange.com/oauth/v3/token"
    _ORANGE_SEND_URL = "https://api.orange.com/smsmessaging/v1/outbound/tel%3A%2B{sender}/requests"

    def __init__(self, cfg: SmsChannel, tz: ZoneInfo, client: httpx.Client) -> None:
        super().__init__(cfg, tz, client)
        env = os.environ.get
        if cfg.provider == "android_gateway":
            self._base = (env(cfg.base_url_env) or "").rstrip("/")
            self._auth = (env(cfg.username_env) or "", env(cfg.password_env) or "")
            if not self._base or not self._auth[0]:
                raise RuntimeError(f"canal {cfg.name} : variables {cfg.base_url_env} et {cfg.username_env} requises")
        else:
            self._orange = (env(cfg.client_id_env) or "", env(cfg.client_secret_env) or "")
            if not all(self._orange):
                raise RuntimeError(f"canal {cfg.name} : variables {cfg.client_id_env} et {cfg.client_secret_env} requises")
            self._token: tuple[str, float] | None = None

    def targets(self) -> list[str]:
        return list(self.cfg.recipients)

    def send(self, alerts, targets):
        text = format_sms(alerts, self._tz, lang=self.cfg.language, templates=self.cfg.templates)
        if self.cfg.provider == "android_gateway":
            # Un seul appel pour tout le groupe : la passerelle envoie un SMS par numéro.
            def go() -> DeliveryError | None:
                res = self._client.post(
                    f"{self._base}/messages", auth=self._auth,
                    json={"phoneNumbers": targets, "textMessage": {"text": text}},
                )
                return None if res.is_success else _http_error(res, "Passerelle SMS")

            outcome = self._call(go)
            return dict.fromkeys(targets, outcome)
        return {number: self._call(lambda n=number: self._orange_send(n, text)) for number in targets}

    def _orange_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        res = self._client.post(self._ORANGE_TOKEN_URL, auth=self._orange, data={"grant_type": "client_credentials"},
                                headers={"Accept": "application/json"})
        if not res.is_success:
            raise _http_error(res, "Orange (jeton)")
        body = res.json()
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _orange_send(self, number: str, text: str) -> DeliveryError | None:
        try:
            token = self._orange_token()
        except DeliveryError as err:
            return err
        sender = (self.cfg.sender or "").lstrip("+")
        res = self._client.post(
            self._ORANGE_SEND_URL.format(sender=sender),
            headers={"Authorization": f"Bearer {token}"},
            json={"outboundSMSMessageRequest": {
                "address": f"tel:{number}", "senderAddress": f"tel:+{sender}",
                "outboundSMSTextMessage": {"message": text}}},
        )
        if res.status_code == 401:
            self._token = None  # jeton refusé : on en redemande un
            return DeliveryError("Orange : jeton refusé", retryable=True)
        return None if res.is_success else _http_error(res, "Orange")


_CHANNELS = {"telegram": TelegramNotifier, "webhook": WebhookNotifier, "sms": SmsNotifier}


# --- Planification et envoi ---


class NotificationDispatcher:
    def __init__(
        self,
        cfg: NotificationsConfig,
        storage: Repository,
        client: httpx.Client | None = None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._cfg = cfg
        self._storage = storage
        self._clock = clock
        self._client = client or httpx.Client(timeout=15.0)
        tz = ZoneInfo(cfg.timezone)
        self.channels: dict[str, Channel] = {c.name: _CHANNELS[c.type](c, tz, self._client) for c in cfg.channels}
        self._sent: dict[str, deque[float]] = defaultdict(deque)  # horodatages des messages envoyés, par canal
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- planification (appelée par la boucle de monitoring, dans la transaction du tick) --------

    def plan(self, alerts: list[Alert]) -> list[Notification]:
        if not self._cfg.enabled:
            return []
        now = self._clock()
        items = []
        for alert in alerts:
            if alert.suppressed:  # maintenance ou cause amont déjà signalée : enregistrée, jamais envoyée
                continue
            payload = alert_to_dict(alert)
            for channel in self.channels.values():
                if channel.accepts(alert):
                    ident = f"{channel.name}:{alert.incident_id}:{alert.timestamp.isoformat()}:{alert.event}"
                    items.append(Notification(id=ident, channel=channel.name, alert=payload, created_at=now,
                                              next_attempt_at=now + self._cfg.batch_window))
        return items

    # -- envoi ------------------------------------------------------------------------------------

    def _allowed(self, channel: Channel, now: datetime) -> datetime | None:
        """None si un message peut partir maintenant, sinon l'heure à laquelle la fenêtre se libère."""
        sent = self._sent[channel.name]
        t = now.timestamp()
        while sent and t - sent[0] > 3600:
            sent.popleft()
        for limit, window in ((channel.cfg.max_messages_per_minute, 60), (channel.cfg.max_messages_per_hour, 3600)):
            if limit is None:
                continue
            recent = [s for s in sent if t - s < window]
            if len(recent) >= limit:
                return datetime.fromtimestamp(recent[-limit] + window, timezone.utc)
        return None

    def run_once(self) -> int:
        """Envoie ce qui est dû. Renvoie le nombre de messages envoyés (un message peut regrouper plusieurs alertes)."""
        now = self._clock()
        messages = 0
        for channel in self.channels.values():
            due = self._storage.due_notifications(channel.name, now)
            groups: dict[tuple[str, ...], list[Notification]] = defaultdict(list)
            for notification in due:
                groups[tuple(notification.pending_targets or channel.targets())].append(notification)
            for targets, batch in groups.items():
                wait_until = self._allowed(channel, now)
                if wait_until is not None:  # plafond atteint : les alertes s'accumulent, un seul message groupé partira plus tard
                    for notification in batch:
                        notification.next_attempt_at = wait_until
                    self._storage.update_notifications(batch)
                    continue
                self._deliver(channel, list(targets), batch, now)
                self._sent[channel.name].append(now.timestamp())
                messages += 1
        return messages

    def _deliver(self, channel: Channel, targets: list[str], batch: list[Notification], now: datetime) -> None:
        batch.sort(key=lambda notification: notification.alert["timestamp"])
        failures = self._send(channel, targets, batch)  # destinataire -> échec, pour ceux qui n'ont pas reçu
        retryable_targets = [target for target, failure in failures.items() if failure.retryable]
        errors = "; ".join(sorted({str(failure) for failure in failures.values()}))[:500] if failures else None
        retry_after = max((failures[target].retry_after or 0 for target in retryable_targets), default=0)
        for notification in batch:
            notification.attempts += 1
            notification.last_error = errors
            if not failures:
                notification.status, notification.sent_at, notification.pending_targets = "sent", now, None
            elif not retryable_targets or notification.attempts >= self._cfg.max_attempts:
                notification.status, notification.pending_targets = "failed", list(failures)
                logger.error("Canal %s : abandon après %d tentative(s) : %s", channel.name, notification.attempts, errors)
            else:
                self._schedule_retry(channel, notification, retryable_targets, retry_after, now, errors)
        self._storage.update_notifications(batch)

    @staticmethod
    def _send(channel: Channel, targets: list[str], batch: list[Notification]) -> dict[str, DeliveryError]:
        """Envoie le lot ; renvoie, par destinataire, l'échec (absent du résultat = bien reçu). Une exception inattendue du canal compte comme un échec à reprendre."""
        try:
            results = channel.send([notification.alert for notification in batch], targets)
        except Exception as exc:
            logger.exception("Canal %s : erreur inattendue", channel.name)
            results = dict.fromkeys(targets, DeliveryError(f"{type(exc).__name__}: {exc}", retryable=True))
        return {target: failure for target in targets if (failure := results.get(target)) is not None}

    def _schedule_retry(
        self, channel: Channel, notification: Notification, retryable_targets: list[str], retry_after: float, now: datetime, errors: str | None
    ) -> None:
        delay = min(self._cfg.retry_base * 2 ** (notification.attempts - 1), _MAX_BACKOFF)
        notification.next_attempt_at = now + max(delay, timedelta(seconds=retry_after))
        notification.pending_targets = retryable_targets
        logger.warning("Canal %s : tentative %d échouée, nouvelle tentative %s : %s", channel.name, notification.attempts, notification.next_attempt_at, errors)

    def send_test(self, channel_name: str) -> dict[str, str | None]:
        """Message d'essai immédiat (hors file) : pour vérifier un identifiant de groupe ou un numéro."""
        channel = self.channels[channel_name]
        probe = {
            "incident_id": "test", "output_id": "TEST", "severity": "critical", "event": "opened",
            "timestamp": self._clock().isoformat(), "message": "Message d'essai", "incident_type": "traffic_drop",
            "started_at": self._clock().isoformat(), "traffic_mbps": 100.0, "expected_traffic_mbps": 250.0,
            "traffic_deviation_percent": -60.0, "loss_percent": 0.1,
        }
        return {t: (str(e) if e else None) for t, e in channel.send([probe], channel.targets()).items()}

    # -- thread ------------------------------------------------------------------------------------

    def start(self, poll_seconds: float = 2.0) -> None:
        if self._thread is not None or not self._cfg.enabled:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(poll_seconds):
                try:
                    self.run_once()
                except Exception:
                    logger.exception("Erreur dans la boucle d'envoi des notifications")

        self._thread = threading.Thread(target=loop, name="notifications", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._client.close()


class Heartbeat:
    """Signal de vie externe (interrupteur d'homme mort). Tant que `check()` ne renvoie aucune raison, un thread
    appelle l'URL du service tiers ; sinon il appelle `url + fail_suffix` avec les raisons en corps. Si le
    processus meurt, les appels cessent : c'est le service tiers qui alerte, pas nous. L'URL est un secret :
    elle vient de l'environnement et n'est jamais journalisée (les erreurs httpx la contiennent parfois)."""

    def __init__(self, cfg: HeartbeatConfig, check: Callable[[], list[str]], client: httpx.Client | None = None) -> None:
        url = os.environ.get(cfg.url_env)
        if not url:
            raise RuntimeError(f"heartbeat.enabled exige la variable d'environnement {cfg.url_env} (URL du service de signal de vie)")
        self._url, self._cfg, self._check = url, cfg, check
        self._client = client or httpx.Client(timeout=cfg.timeout.total_seconds())
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def beat(self) -> bool:
        """Un appel. Renvoie vrai si le service tiers l'a reçu (jamais d'exception : un service injoignable est journalisé)."""
        try:
            reasons = self._check()
        except Exception as exc:
            logger.exception("Signal de vie : le contrôle de santé a échoué")
            reasons = [f"contrôle de santé en erreur ({type(exc).__name__})"]
        url = self._url.rstrip("/") + self._cfg.fail_suffix if reasons else self._url
        try:
            self._client.post(url, content="; ".join(reasons)[:1000].encode()).raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Signal de vie : le service a répondu %s", exc.response.status_code)
            return False
        except httpx.HTTPError as exc:
            logger.warning("Signal de vie : service injoignable (%s)", type(exc).__name__)
            return False
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while True:
                self.beat()
                if self._stop.wait(self._cfg.period.total_seconds()):
                    return

        self._thread = threading.Thread(target=loop, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._client.close()


def build_dispatcher(cfg: NotificationsConfig, storage: Repository) -> NotificationDispatcher | None:
    """None si les notifications sont désactivées. Lève `RuntimeError` (message clair) si un
    secret manque : mieux vaut refuser de démarrer que découvrir pendant un incident qu'aucune alerte ne part."""
    return NotificationDispatcher(cfg, storage) if cfg.enabled else None


__all__ = ["Heartbeat", "NotificationDispatcher", "build_dispatcher", "format_sms", "format_text"]
