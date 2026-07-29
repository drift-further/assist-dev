"""shared/studio_client.py — HTTP client for the Studio design hub.

All Studio traffic is server->server so the API token never reaches the browser
(effort 268: server-proxied client). Nothing here raises: every call returns an
HTTP status code, with 0 meaning "could not reach Studio at all". Callers decide
what a failure means; the UI always degrades to the last good snapshot.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from shared import state

TIMEOUT = 3.0
_PROJECTS_TTL = 30.0


def _setting(key, default=""):
    try:
        val = state.get_setting("studio", key)
    except (KeyError, TypeError):
        return default
    return default if val is None else val


class StudioClient:
    """Thin wrapper over Studio's HTTP API. Safe to share across threads."""

    def __init__(self):
        self._projects = {"at": 0.0, "data": []}
        self._lock = threading.Lock()

    # --- configuration -------------------------------------------------
    def api_base(self):
        return str(_setting("api_base", "")).strip().rstrip("/")

    def web_base(self):
        """Where the BROWSER should open Studio.

        An empty web_base derives from api_base — Studio serves its SPA on the
        same origin as its API, so one URL is enough for the common install.
        """
        web = str(_setting("web_base", "")).strip().rstrip("/")
        return web or self.api_base()

    def token(self):
        return str(_setting("api_token", "")).strip()

    def configured(self):
        return bool(self.api_base())

    # --- transport ------------------------------------------------------
    def request(self, method, path, payload=None, timeout=TIMEOUT):
        """Return (status_code, parsed_json_or_None). Never raises.

        code 0 == unreachable (DNS, refused, timeout, torn response).
        """
        base = self.api_base()
        if not base:
            return 0, None
        # EVERYTHING is inside the try, including Request construction and JSON
        # serialization. A typo'd api_base ("studio.drift" with no scheme — easy
        # to paste into the connect sheet) makes Request() raise ValueError; an
        # exception escaping here is caught by the refresher's bare `except`,
        # which leaves the snapshot frozen on its last state, so the UI reports
        # `connected` forever while nothing works.
        try:
            req = urllib.request.Request(base + path, method=method)
            tok = self.token()
            if tok:
                # Forward-compatible with studio 232-W1; harmless against today's
                # unauthenticated loopback Studio, which ignores the header.
                req.add_header("Authorization", "Bearer " + tok)
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, body, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, (json.loads(raw) if raw else None)
                except ValueError:
                    return resp.status, None
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return 0, None

    # --- endpoints ------------------------------------------------------
    def health(self):
        """HTTP status of GET /api/health. 200 == Studio is up and answering."""
        code, _ = self.request("GET", "/api/health")
        return code

    def inbox(self):
        """(code, items) — (blocking AND pending) OR recommended, newest first."""
        code, data = self.request("GET", "/api/questions/inbox")
        return code, data if isinstance(data, list) else []

    def answer(self, question_id, answer_json, status="answered", actor="user"):
        """POST an answer. answer_json shape matches the sto CLI:
        {"text": str, "selections": [str], "rationale": str} — the SPA and the
        finding-adjudication loop both read `selections`, so the key is a
        contract, not a preference."""
        return self.request(
            "POST",
            "/api/questions/%d/answer" % int(question_id),
            {"answer_json": answer_json, "status": status, "actor": actor},
        )

    def effort(self, effort_id):
        code, data = self.request("GET", "/api/efforts/%d" % int(effort_id))
        return data if code == 200 and isinstance(data, dict) else None

    # --- projects (cached; the refresher warms this) ---------------------
    def projects(self):
        """Studio's project list, refetched at most every 30s.

        Studio returns a bare array unpaginated and {"items": [...]} when a
        paging param is present — accept both shapes so a contract change
        cannot silently empty the cwd->project resolver. On any failure the
        previous list is kept (fail soft).
        """
        now = time.time()
        with self._lock:
            if now - self._projects["at"] < _PROJECTS_TTL:
                return list(self._projects["data"])
        code, data = self.request("GET", "/api/projects?limit=500")
        if isinstance(data, dict):
            rows = data.get("items") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if code == 200:
            with self._lock:
                self._projects = {"at": now, "data": rows}
            return list(rows)
        with self._lock:
            return list(self._projects["data"])

    def cached_projects(self):
        """Whatever projects() last stored. NEVER makes a request — this is the
        accessor /poll uses, so the 5s poll can't block on Studio."""
        with self._lock:
            return list(self._projects["data"])


client = StudioClient()
