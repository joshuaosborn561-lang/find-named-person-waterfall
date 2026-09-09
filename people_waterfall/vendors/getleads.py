"""GetLeads decision-maker search. Domain required. Unlimited-plan $0.

Uses job_titles from the profile and exact company_size band labels.
Never numeric employee bounds. require_email=false. Drop stale/former rows.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, is_current_employment, person_from_row
from people_waterfall.profile import ClientProfile


class GetLeadsClient:
    tier = "getleads"

    def __init__(self, api_key: str | None = None, timeout: int = 60):
        self.api_key = api_key if api_key is not None else settings.getleads_api_key
        self.base_url = settings.getleads_base_url.rstrip("/")
        self.people_path = settings.getleads_people_path
        self.upload_url = settings.getleads_upload_url
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        url = path if path.startswith("http") else f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        self.calls += 1
        r = http_client.post(
            self.tier,
            url,
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else {"data": data}

    def _absorb(self, data: Any, limit: int) -> list[PersonHit]:
        if data is None:
            return []
        rows: Any
        if isinstance(data, list):
            rows = data
        else:
            rows = (
                data.get("people")
                or data.get("contacts")
                or data.get("data")
                or data.get("results")
                or []
            )
        if isinstance(rows, dict):
            rows = rows.get("people") or rows.get("results") or rows.get("data") or []
        out: list[PersonHit] = []
        for row in rows[: max(limit * 3, limit)]:
            if not isinstance(row, dict):
                continue
            if is_current_employment(row) is False:
                continue
            person = person_from_row(row, self.tier)
            if person:
                out.append(person)
            if len(out) >= limit:
                break
        return out

    def find_people(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        limit: int = 25,
    ) -> list[PersonHit]:
        if not self.enabled or not domain:
            return []
        titles = titles or list(profile.target_titles)
        body: dict[str, Any] = {
            "domain": domain,
            "company_name": company_name or domain,
            "job_titles": titles,
            "titles": titles,
            "require_email": False,
            "limit": limit,
        }
        if profile.company_size:
            body["company_size"] = list(profile.company_size)
        if profile.employee_profiles_min is not None:
            body["employee_profiles_on_linkedin_min"] = profile.employee_profiles_min
        if profile.employee_profiles_max is not None:
            body["employee_profiles_on_linkedin_max"] = profile.employee_profiles_max
        data = self._post(self.people_path, body)
        if data is None:
            data = self._post("/search-contacts", body)
        people = self._absorb(data, limit)
        if people:
            self.hits += 1
        return people

    def batch_domains(
        self,
        domains: list[str],
        *,
        profile: ClientProfile,
        per_domain_limit: int = 50,
    ) -> dict[str, Any]:
        """Server-side upload to a getleads-upload edge function when configured.

        Returns {upload_url?, run_id?, submitted, status}. Caller polls.
        """
        clean = [d.strip().lower() for d in domains if d and d.strip()]
        if not clean:
            return {"submitted": 0, "status": "empty"}
        if not self.upload_url:
            return {
                "submitted": 0,
                "status": "no_upload_url",
                "note": "GETLEADS_UPLOAD_URL unset; use per-domain find_people",
            }
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["domain"])
        for domain in clean:
            writer.writerow([domain])
        payload = {
            "filename": "people-waterfall-domains.csv",
            "csv": buf.getvalue(),
            "job_titles": list(profile.target_titles),
            "company_size": list(profile.company_size),
            "require_email": False,
            "per_domain_limit": per_domain_limit,
        }
        self.calls += 1
        r = http_client.post(
            self.tier,
            self.upload_url,
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            return {"submitted": 0, "status": "upload_failed"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        return {
            "submitted": len(clean),
            "status": str((data or {}).get("status") or "submitted"),
            "run_id": (data or {}).get("run_id") or (data or {}).get("id"),
            "upload_url": (data or {}).get("upload_url"),
        }

    def poll_batch(self, run_id: str, *, timeout_s: int = 3600) -> list[PersonHit]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data = self._post("/decision-makers-batch/status", {"run_id": run_id})
            if not data:
                time.sleep(15)
                continue
            status = str(data.get("status") or "").lower()
            if status in {"completed", "done", "success"}:
                return self._absorb(data, 10_000)
            if status in {"failed", "error"}:
                return []
            time.sleep(15)
        return []
