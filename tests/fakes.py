"""Shared vendor fakes. Counts only — no company names in assertions elsewhere."""

from people_waterfall.vendors.site_people import SitePeopleBatchResult, SitePeopleDomainResult


class EmptySitePeople:
    enabled = True
    calls = 0

    def resolve_domains(self, domains, **kwargs):
        self.calls += 1
        by = {d: SitePeopleDomainResult(domain=d) for d in domains}
        return SitePeopleBatchResult(
            by_domain=by,
            loaded=len(by),
            fetched=0,
            extracted=0,
            picked=0,
            batch=str(kwargs.get("batch") or ""),
        )


class EmptyGetleads:
    enabled = True
    calls = 0
    last_error = ""

    def find_people(self, **kwargs):
        self.calls += 1
        return []

    def search_people(self, domains, **kwargs):
        self.calls += max(1, len(list(domains)))
        return {d: [] for d in domains}
