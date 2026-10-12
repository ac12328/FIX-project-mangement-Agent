"""
Microsoft Graph API client — auth, read, send.
Uses MSAL client_credentials (app-only) flow with in-memory token cache.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_POLL_STATE_FILE = "graph_poll_state.json"


class GraphClient:
    """
    Thin HTTP client for Microsoft Graph email endpoints.
    Manages token acquisition and caching transparently.

    Auth flow: MSAL client_credentials (app-only, no interactive login).
    Requires Azure AD app registration with Mail.Read and Mail.Send
    Application (not Delegated) permissions, plus admin consent.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        user_email: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_email = user_email
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._msal_app = None  # lazy-initialised on first token request

    # ── Auth ──────────────────────────────────────────────────────

    def _get_msal_app(self):
        """Lazy-init MSAL ConfidentialClientApplication."""
        if self._msal_app is None:
            import msal
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            )
        return self._msal_app

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if within 60 s of expiry."""
        now = time.monotonic()
        if self._token and now < self._token_expiry - 60:
            return self._token
        result = self._get_msal_app().acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(
                "MSAL token acquisition failed: "
                + result.get("error_description", result.get("error", "unknown"))
            )
        self._token = result["access_token"]
        self._token_expiry = now + result.get("expires_in", 3600)
        return self._token

    # ── Internal HTTP helpers ──────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
        }

    def _get(self, url: str, params: dict | None = None, *, immutable_ids: bool = False) -> dict:
        import requests
        headers = self._headers()
        if immutable_ids:
            headers["Prefer"] = 'IdType="ImmutableId"'
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, url: str, payload: dict) -> dict:
        import requests
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, url: str, payload: dict) -> dict:
        import requests
        resp = requests.patch(url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _delete(self, url: str) -> None:
        import requests
        resp = requests.delete(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()

    def _post_no_body(self, url: str, payload: dict) -> None:
        """POST where a successful 202 has no response body (e.g. sendMail)."""
        import requests
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()

    # ── Read ──────────────────────────────────────────────────────

    def fetch_new_messages(
        self,
        folder: str = "inbox",
        since_datetime: str | None = None,
    ) -> list[dict]:
        """
        Fetch all matching pages from a mail folder, newest first.

        Args:
            folder: Graph well-known folder name (e.g. "inbox", "sentitems").
            since_datetime: ISO 8601 UTC string — if given, only returns
                messages with receivedDateTime newer than this value.

        Returns:
            List of normalised message dicts:
            {email_id, graph_id, sender_email, subject, body,
             received_at, thread_id, in_reply_to, references}
        """
        url = (
            f"{GRAPH_BASE}/users/{self.user_email}"
            f"/mailFolders/{folder}/messages"
        )
        params: dict = {
            "$select": (
                "id,subject,receivedDateTime,sentDateTime,from,body,"
                "internetMessageId,conversationId,internetMessageHeaders,"
                "toRecipients,ccRecipients,bccRecipients"
            ),
            "$orderby": "receivedDateTime desc",
            "$top": 50,
        }
        if since_datetime:
            params["$filter"] = f"receivedDateTime gt {since_datetime}"

        messages = []
        while url:
            data = self._get(url, params=params, immutable_ids=True)
            messages.extend(self._normalise_message(msg) for msg in data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None
        return messages

    def fetch_message_by_id(self, graph_id: str) -> dict:
        """Re-fetch a captured message by immutable Graph ID across folder moves."""
        if not graph_id:
            raise ValueError("A Graph message ID is required")
        url = f"{GRAPH_BASE}/users/{self.user_email}/messages/{quote(graph_id, safe='')}"
        params = {"$select": (
            "id,subject,receivedDateTime,sentDateTime,from,body,"
            "internetMessageId,conversationId,internetMessageHeaders,"
            "toRecipients,ccRecipients,bccRecipients"
        )}
        return self._normalise_message(self._get(url, params=params, immutable_ids=True))

    @staticmethod
    def _normalise_message(msg: dict) -> dict:
        headers_map = {
            h["name"].lower(): h["value"]
            for h in msg.get("internetMessageHeaders") or []
        }
        def graph_recipients(key: str) -> list[dict]:
            recipients = []
            for raw in msg.get(key) or []:
                address = raw.get("emailAddress", {}).get("address")
                if not address:
                    continue
                recipients.append({
                    "email": address,
                    "display_name": raw.get("emailAddress", {}).get("name") or None,
                    "contact_id": None,
                    "source": "thread_participant",
                })
            return recipients

        to_recipients = graph_recipients("toRecipients")
        cc_recipients = graph_recipients("ccRecipients")
        bcc_recipients = graph_recipients("bccRecipients")
        to_emails = [item["email"].lower() for item in to_recipients]
        cc_addresses = [item["email"] for item in cc_recipients]
        bcc_addresses = [item["email"] for item in bcc_recipients]
        return {
            "email_id": msg.get("internetMessageId") or msg["id"],
            "graph_id": msg["id"],
            "sender_email": (
                msg.get("from", {})
                .get("emailAddress", {})
                .get("address", "")
            ),
            "subject": msg.get("subject") or "",
            "body": msg.get("body", {}).get("content") or "",
            "received_at": msg.get("receivedDateTime") or "",
            "sent_at": msg.get("sentDateTime"),
            "thread_id": msg.get("conversationId"),
            "in_reply_to": headers_map.get("in-reply-to"),
            "references": headers_map.get("references"),
            "authentication_headers": {
                key: value for key, value in headers_map.items()
                if key in {"authentication-results", "arc-authentication-results", "from", "return-path"}
            },
            "to_emails": to_emails,
            "to_recipients": to_recipients,
            "cc": ", ".join(cc_addresses) or None,
            "cc_recipients": cc_recipients,
            "bcc": ", ".join(bcc_addresses) or None,
            "bcc_recipients": bcc_recipients,
        }

    def fetch_calendar_view(
        self,
        *,
        calendar_id: str,
        start_datetime: str,
        end_datetime: str,
    ) -> list[dict]:
        """Fetch calendar events that overlap an explicit UTC time window."""
        calendar_path = (
            "/calendar/calendarView"
            if calendar_id == "calendar"
            else f"/calendars/{calendar_id}/calendarView"
        )
        url = f"{GRAPH_BASE}/users/{self.user_email}{calendar_path}"
        params = {
            "startDateTime": start_datetime,
            "endDateTime": end_datetime,
            "$select": (
                "id,subject,start,end,location,organizer,attendees,"
                "isOnlineMeeting,onlineMeeting,onlineMeetingUrl,showAs,"
                "sensitivity,bodyPreview,lastModifiedDateTime,isCancelled"
            ),
            "$orderby": "start/dateTime",
            "$top": 250,
        }
        events: list[dict] = []
        while url:
            data = self._get(url, params=params)
            events.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None
        return events

    def fetch_calendar_event(self, *, calendar_id: str, event_id: str) -> dict:
        """Fetch one current Graph event for stale-approval validation."""
        url = self._calendar_event_url(calendar_id, event_id)
        return self._get(url, params={
            "$select": (
                "id,subject,start,end,location,organizer,attendees,body,"
                "isOnlineMeeting,onlineMeeting,showAs,sensitivity,"
                "lastModifiedDateTime,isCancelled"
            )
        })

    def create_calendar_event(self, *, calendar_id: str, payload: dict) -> dict:
        calendar_path = (
            "/calendar/events"
            if calendar_id == "calendar"
            else f"/calendars/{calendar_id}/events"
        )
        return self._post(
            f"{GRAPH_BASE}/users/{self.user_email}{calendar_path}", payload
        )

    def update_calendar_event(
        self, *, calendar_id: str, event_id: str, payload: dict
    ) -> dict:
        return self._patch(self._calendar_event_url(calendar_id, event_id), payload)

    def cancel_calendar_event(self, *, calendar_id: str, event_id: str) -> None:
        self._delete(self._calendar_event_url(calendar_id, event_id))

    def _calendar_event_url(self, calendar_id: str, event_id: str) -> str:
        calendar_path = (
            f"/calendar/events/{event_id}"
            if calendar_id == "calendar"
            else f"/calendars/{calendar_id}/events/{event_id}"
        )
        return f"{GRAPH_BASE}/users/{self.user_email}{calendar_path}"

    # ── Send ──────────────────────────────────────────────────────

    def send_message(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        *,
        body_html: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        save_to_sent_items: bool = True,
    ) -> str:
        """Send an HTML-capable multi-recipient email through Graph sendMail."""
        from email.utils import getaddresses

        def _addresses(value: str | list[str] | None) -> list[str]:
            if not value:
                return []
            values = [value] if isinstance(value, str) else value
            return [address for _name, address in getaddresses(values) if address]

        url = f"{GRAPH_BASE}/users/{self.user_email}/sendMail"
        message: dict = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if body_html else "Text",
                "content": body_html or body,
            },
            "toRecipients": [
                {"emailAddress": {"address": address}} for address in _addresses(to)
            ],
        }
        cc_addresses = _addresses(cc)
        bcc_addresses = _addresses(bcc)
        if cc_addresses:
            message["ccRecipients"] = [
                {"emailAddress": {"address": address}} for address in cc_addresses
            ]
        if bcc_addresses:
            message["bccRecipients"] = [
                {"emailAddress": {"address": address}} for address in bcc_addresses
            ]

        internet_headers = []
        if in_reply_to:
            internet_headers.append({"name": "In-Reply-To", "value": in_reply_to})
        if references:
            internet_headers.append({"name": "References", "value": references})
        if body_html:
            internet_headers.append({"name": "X-AgentFIX-Plain-Fallback", "value": body[:512]})
        if internet_headers:
            message["internetMessageHeaders"] = internet_headers

        self._post_no_body(
            url,
            {"message": message, "saveToSentItems": save_to_sent_items},
        )
        now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"<graph-{now_iso}@{self.user_email}>"


    # ── Poll state helpers ─────────────────────────────────────────

    def save_last_poll_time(
        self, data_dir: str, folder: str, iso_ts: str
    ) -> None:
        """Persist the last successful poll timestamp to data/graph_poll_state.json."""
        poll_file = Path(data_dir) / _POLL_STATE_FILE
        state: dict = {}
        if poll_file.exists():
            try:
                state = json.loads(poll_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        state[folder] = iso_ts
        poll_file.parent.mkdir(parents=True, exist_ok=True)
        poll_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_last_poll_time(self, data_dir: str, folder: str) -> str | None:
        """Load last successful poll timestamp. Returns None on first run."""
        poll_file = Path(data_dir) / _POLL_STATE_FILE
        if not poll_file.exists():
            return None
        try:
            state = json.loads(poll_file.read_text(encoding="utf-8"))
            return state.get(folder)
        except Exception:
            return None
