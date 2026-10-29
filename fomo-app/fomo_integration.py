"""FOMOAPI REST adapter. Runs only in paper mode and never signs/submits orders."""
from __future__ import annotations
import json, os, threading, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.fomoapi.io"
VALID_WINDOWS = ("24h", "7d", "30d", "all")

def stamp_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def iso_from_event(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            return None
    return None

class FomoFeed:
    def __init__(self, key, on_alert, data_dir, top_n=5):
        self.key, self.on_alert = key, on_alert
        self.data_dir = Path(data_dir)
        self.top_n = max(1, min(int(top_n), 20))
        self.settings_path = self.data_dir / "fomo_settings.json"
        self.windows = ["24h", "7d"]
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            saved_windows = saved.get("windows", self.windows)
            if isinstance(saved_windows, list) and saved_windows and all(w in VALID_WINDOWS for w in saved_windows): self.windows = list(dict.fromkeys(saved_windows))
            self.top_n = max(1, min(int(saved.get("top_n", self.top_n)), 20))
        except (OSError, ValueError, TypeError, AttributeError): pass
        self.state = {"configured": bool(key), "connected": False, "message": "Clave no configurada", "last_poll_at": None, "last_alert_at": None, "last_error": None, "delay_seconds": None, "credits_remaining": None, "credits_cost": None, "top_n": self.top_n, "windows": self.windows}
        self.auto_copy = os.environ.get('FOMO_AUTO_COPY','1').lower() not in ('0','false','no','off')
        self.investors = []
        self.selected_ids = set()
        self.cursor_path = self.data_dir / "fomo_cursor.json"
        self.cursor = None
        self.processed_ids = set()
        self.last_board_refresh = 0
        self.thread = None
        if key:
            try:
                stored = json.loads(self.cursor_path.read_text(encoding="utf-8"))
                self.cursor = stored.get("since")
                self.processed_ids = set(stored.get("processed", []))
            except (OSError, ValueError, AttributeError):
                self.cursor = None
            self.state["message"] = "Conectando al proveedor de datos de FOMO…"

    def _get(self, path, params=None):
        url = BASE + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers={"Authorization": "Bearer " + self.key, "Accept": "application/json", "User-Agent": "FomoPaper/1.0"})
        try:
            with urlopen(req, timeout=15) as response:
                self.state["credits_remaining"] = response.headers.get("x-credits-remaining") or self.state.get("credits_remaining")
                self.state["credits_cost"] = response.headers.get("x-credits-cost") or self.state.get("credits_cost")
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"FOMOAPI respondió HTTP {exc.code}: {detail}") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"No se pudo conectar con FOMOAPI: {exc}") from None

    def api_get(self, path, params=None):
        return self._get(path, params)

    def _board_rows(self, window):
        body = self._get(f"/v2/leaderboard/{window}", {"limit": 150})
        return body.get("traders", body.get("data", {}).get("traders", [])) if isinstance(body, dict) else []

    def set_preferences(self, windows, top_n):
        self.windows = list(dict.fromkeys(windows)); self.top_n = max(1, min(int(top_n), 20))
        self.state.update({"windows": self.windows, "top_n": self.top_n})
        self.settings_path.write_text(json.dumps({"windows": self.windows, "top_n": self.top_n}, indent=2), encoding="utf-8")
        self.last_board_refresh = 0

    def refresh_leaderboard(self):
        boards = {window: self._board_rows(window) for window in self.windows}
        by_id = {}
        for window, rows in boards.items():
            rank_key = {"24h":"rank_24h", "7d":"rank_7d", "30d":"rank_30d", "all":"rank_all"}[window]
            pnl_key = {"24h":"pnl_24h_usd", "7d":"pnl_7d_usd", "30d":"pnl_30d_usd", "all":"pnl_all_usd"}[window]
            for row in rows:
                uid = row.get("userId") or row.get("fomoUserId")
                if not uid: continue
                item=by_id.setdefault(str(uid), {"user_id":str(uid),"handle":row.get("handle",str(uid)),"name":row.get("displayName") or row.get("handle",str(uid))})
                item.update({rank_key:row.get("rank",row.get("allTimeRank")),pnl_key:row.get("pnlUsd"),"verified":row.get("verified",False)})
        candidates = []
        for item in by_id.values():
            rank_keys=[{"24h":"rank_24h","7d":"rank_7d","30d":"rank_30d","all":"rank_all"}[w] for w in self.windows]
            ranks=[item.get(k) for k in rank_keys]
            if any(r is None for r in ranks): continue
            # Equal-weight average percentile for the selected windows.
            item["score"] = round(sum(100 * (151-int(r))/150 for r in ranks) / len(ranks), 1)
            candidates.append(item)
        candidates.sort(key=lambda item: (-item["score"], str(item["user_id"])))
        self.investors = candidates[:self.top_n]
        self.selected_ids = {str(x["user_id"]) for x in self.investors}
        self.state.update({"windows": self.windows, "top_n": self.top_n})
        self.last_board_refresh = time.monotonic()
        return len(candidates)

    @staticmethod
    def _events(body):
        if isinstance(body, list):
            return body
        if not isinstance(body, dict):
            return []
        for key in ("alerts", "events", "items", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested in ("alerts", "events", "items"):
                    if isinstance(value.get(nested), list):
                        return value[nested]
        return []

    def _persist_cursor(self):
        try:
            self.cursor_path.write_text(json.dumps({"since": self.cursor, "processed": list(self.processed_ids)[-5000:]}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def start(self):
        if self.key and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, name="fomo-paper-feed", daemon=True)
            self.thread.start()

    def _run(self):
        if not self.cursor:
            self.cursor = stamp_now()
            self._persist_cursor()  # Do not replay old trades as if they were detected now.
        while True:
            try:
                if time.monotonic() - self.last_board_refresh > 21600 or not self.investors:
                    count = self.refresh_leaderboard()
                    self.state["leaderboard_count"] = count
                body = self._get("/v2/alerts", {"since": self.cursor, "limit": 100})
                events = self._events(body)
                events.sort(key=lambda e: iso_from_event(e.get("ts")) or "")
                for event in events:
                    event_id = event.get("eventId")
                    event_time = iso_from_event(event.get("execTs") or event.get("ts"))
                    uid = str(event.get("userId") or "")
                    if event_time and (not self.cursor or event_time > self.cursor):
                        self.cursor = event_time
                    if not uid or uid not in self.selected_ids or not event_id or event_id in self.processed_ids:
                        continue
                    typ = str(event.get("alertType") or event.get("type") or "").lower()
                    if typ not in ("buy", "sell", "swap_buy", "swap_sell"):
                        continue
                    event["_side"] = "BUY" if "buy" in typ else "SELL"
                    event["_event_time"] = event_time
                    try:
                        if self.auto_copy:
                            self.on_alert(event)
                            self.state["last_alert_at"] = stamp_now()
                    except Exception as exc:
                        self.state["last_error"] = str(exc)
                    self.processed_ids.add(str(event_id))
                    if len(self.processed_ids)>5000:
                        self.processed_ids=set(list(self.processed_ids)[-4000:])
                if not events:
                    self.cursor = stamp_now()
                self._persist_cursor()
                self.state.update({"connected": True, "message": "Conectado · polling cada 15 s", "last_poll_at": stamp_now(), "last_error": None})
            except Exception as exc:
                self.state.update({"connected": False, "message": str(exc), "last_error": str(exc), "last_poll_at": stamp_now()})
            time.sleep(15)
