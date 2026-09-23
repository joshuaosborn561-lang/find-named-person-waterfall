"""Resolve named people. Never finds an email. Profile-driven, cheapest first."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import config as cfg
from .geo import apply_person_geo
from .handoff import handoff_title_matches
from .people import PersonHit, company_matches, looks_like_person
from .pricing import (
    LiveRates,
    compute_tier_order,
    lane_tiers,
    parse_tier_list,
    select_tiers,
)
from .profile import ClientProfile, get_profile, normalize_client_tag
from .progress import counter_from_stats
from .source import (
    TableSource,
    count_source,
    ensure_people_writeback,
    iter_source,
    parse_source,
    writeback_people,
)
from .titles import audit_title
from .vendors.ai_ark import AiArkClient
from .vendors.ai_ark import per_credit_from_payload as ark_per_credit
from .vendors.cache import CacheClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient
from .vendors.leadmagic import per_credit_from_payload as lm_per_credit
from .vendors.prospeo import ProspeoClient
from .vendors.prospeo import per_credit_from_payload as prospeo_per_credit
from .vendors.serp import SerpClient, SerpQuery, build_query
from .vendors.smartlead import SmartleadClient
from .write import (
    contact_payload,
    is_known,
    load_known_names,
    write_contacts,
    write_name_bank,
)

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass
class VendorBundle:
    cache: CacheClient
    getleads: GetLeadsClient
    smartlead: SmartleadClient
    leadmagic: LeadMagicClient
    aiark: AiArkClient
    serp: SerpClient
    prospeo: ProspeoClient

    def for_tier(self, tier: str) -> Any:
        return {
            "cache": self.cache,
            "getleads": self.getleads,
            "smartlead": self.smartlead,
            "leadmagic_employee": self.leadmagic,
            "leadmagic_role": self.leadmagic,
            "aiark": self.aiark,
            "serp": self.serp,
            "prospeo": self.prospeo,
        }.get(tier)


def build_vendors() -> VendorBundle:
    return VendorBundle(
        cache=CacheClient(),
        getleads=GetLeadsClient(),
        smartlead=SmartleadClient(),
        leadmagic=LeadMagicClient(),
        aiark=AiArkClient(),
        serp=SerpClient(),
        prospeo=ProspeoClient(),
    )


def read_live_rates(
    vendors: VendorBundle,
    *,
    probe_search: bool = False,
    stored_search_free: bool | None = None,
) -> LiveRates:
    rates = LiveRates()
    rates.leadmagic_search_free = stored_search_free
    if vendors.leadmagic.enabled:
        payload = vendors.leadmagic.credits()
        rates.leadmagic_credits = None
        per, plan = lm_per_credit(payload)
        rates.leadmagic_per_credit = per
        rates.leadmagic_plan = plan
        bal = payload.get("credits") or payload.get("remaining") or payload.get("balance")
        if isinstance(bal, (int, float)):
            rates.leadmagic_credits = float(bal)
        elif isinstance(bal, dict) and isinstance(bal.get("remaining"), (int, float)):
            rates.leadmagic_credits = float(bal["remaining"])
        if rates.leadmagic_per_credit is None:
            rates.leadmagic_per_credit = 0.0198
            rates.notes.append("leadmagic per-credit defaulted to Essential midpoint")
        if probe_search and stored_search_free is None:
            probe = vendors.leadmagic.probe_search_free()
            rates.leadmagic_search_free = probe
            if probe is True:
                rates.notes.append("leadmagic role/search billed 0 credits on a miss probe")
            elif probe is False:
                rates.notes.append("leadmagic role-finder spent credits on the probe")
            else:
                rates.notes.append("leadmagic search-free probe inconclusive")
    if vendors.aiark.enabled:
        payload = vendors.aiark.credits()
        per, credits = ark_per_credit(payload)
        rates.aiark_per_credit = per or 0.0049
        rates.aiark_credits = credits
        if per is None:
            rates.notes.append("aiark per-credit defaulted to published high end")
    if vendors.prospeo.enabled:
        payload = vendors.prospeo.credits()
        per, credits = prospeo_per_credit(payload)
        rates.prospeo_per_credit = per or 0.023
        rates.prospeo_credits = credits
    if vendors.smartlead.enabled:
        vendors.smartlead.refresh_credits()
    return rates


def _call_tier(
    tier: str,
    vendors: VendorBundle,
    *,
    profile: ClientProfile,
    domain: str,
    company_name: str,
    city: str,
    state: str,
    titles: list[str],
    first_name: str = "",
    last_name: str = "",
) -> list[PersonHit]:
    client = vendors.for_tier(tier)
    if client is None or not getattr(client, "enabled", True):
        return []
    kwargs: dict[str, Any] = {
        "profile": profile,
        "domain": domain,
        "company_name": company_name,
        "city": city,
        "state": state,
        "titles": titles,
    }
    if tier == "leadmagic_employee":
        return vendors.leadmagic.employee_finder(**kwargs)
    if tier == "leadmagic_role":
        return vendors.leadmagic.find_people_by_role(**kwargs)
    if tier == "smartlead":
        return vendors.smartlead.find_people(
            **kwargs, first_name=first_name, last_name=last_name
        )
    return client.find_people(**kwargs)


def _lane_order(
    allowed: list[str],
    order: list[dict[str, Any]],
    lane: str,
    company_name: str,
) -> list[str]:
    """SERP is company+title search. Include it whenever it is allowed and
    the row has a company name, even on the domain lane."""
    lane_set = set(lane_tiers(order, lane))
    out: list[str] = []
    for tier in allowed:
        if tier in lane_set:
            out.append(tier)
        elif tier == "serp" and company_name:
            out.append(tier)
    return out


def _split_lane(lane_order: list[str]) -> tuple[list[str], bool, list[str]]:
    if "serp" not in lane_order:
        return list(lane_order), False, []
    idx = lane_order.index("serp")
    return lane_order[:idx], True, lane_order[idx + 1 :]


@dataclass
class _CompanyWork:
    row: dict[str, Any]
    domain: str
    company: str
    city: str
    state: str
    first: str
    last: str
    lane_order: list[str]
    before: list[str]
    has_serp: bool
    after: list[str]
    accepted_rows: list[dict[str, Any]] = field(default_factory=list)
    bank_count: int = 0
    last_source: str = ""
    used_fallback: bool = False
    deferred: bool = False


def _audit_people(
    people: list[PersonHit],
    *,
    profile: ClientProfile,
    company_name: str,
    domain: str,
    use_fallback: bool,
) -> tuple[list[tuple[PersonHit, Any, float]], list[PersonHit]]:
    accepted: list[tuple[PersonHit, Any, float]] = []
    bank: list[PersonHit] = []
    current = [p for p in people if p.is_current is True]
    unknown = [p for p in people if p.is_current is None]
    pool = current or unknown or people
    for person in pool:
        if not looks_like_person(person.first_name, person.last_name):
            continue
        if not company_matches(
            input_company=company_name,
            input_domain=domain,
            returned_company=person.company_name,
            returned_domain=person.domain,
        ):
            if domain and person.domain and person.domain != domain:
                continue
            if company_name and person.company_name:
                continue
            if domain and not person.domain and not person.company_name:
                person.domain = domain
                person.company_name = company_name
            elif not company_matches(
                input_company=company_name,
                input_domain=domain,
                returned_company=person.company_name or company_name,
                returned_domain=person.domain or domain,
            ):
                continue
        geo = apply_person_geo(
            person_state=person.person_state,
            person_city=person.person_city,
            company_state="",
            geo=profile.geo,
            default_confidence=person.source_confidence,
        )
        if not geo.keep:
            continue
        audit = audit_title(
            person.title,
            target_titles=profile.target_titles,
            title_synonyms=profile.title_synonyms,
            title_exclude_regex=profile.title_exclude_regex,
            seniority_floor=profile.seniority_floor,
            fallback_titles=profile.fallback_titles,
            use_fallback=use_fallback,
        )
        if audit.title_match:
            accepted.append((person, audit, geo.confidence))
        else:
            bank.append(person)
    return accepted, bank


def estimate_job(
    src: TableSource,
    *,
    profile: ClientProfile,
    rates: LiveRates,
    order: list[dict[str, Any]],
    tiers: list[str],
) -> dict[str, Any]:
    rows = count_source(src)
    per_tier: list[dict[str, Any]] = []
    total = 0.0
    for row in order:
        if row["tier"] not in tiers:
            continue
        unit = float(row.get("unit_usd") or 0)
        billing = row.get("billing")
        if billing == "free":
            cost = 0.0
        elif billing == "free_on_miss":
            rate = row.get("measured_rate")
            cost = unit * rows * (rate if rate is not None else 0.5)
        elif row["tier"] == "serp":
            cost = unit * rows
        else:
            cost = unit * rows
        total += cost
        per_tier.append(
            {
                "tier": row["tier"],
                "rows": rows,
                "unit_usd": unit,
                "billing": billing,
                "estimated_usd": round(cost, 4),
                "needs": row.get("needs"),
            }
        )
    return {
        "ok": True,
        "estimate_only": True,
        "client_tag": profile.client_tag,
        "source_table": src.qualified,
        "rows": rows,
        "selected_tiers": list(tiers),
        "tiers": per_tier,
        "estimated_usd": round(total, 4),
        "live_rates": {
            "leadmagic_per_credit": rates.leadmagic_per_credit,
            "leadmagic_credits": rates.leadmagic_credits,
            "leadmagic_plan": rates.leadmagic_plan,
            "leadmagic_search_free": rates.leadmagic_search_free,
            "aiark_per_credit": rates.aiark_per_credit,
            "prospeo_per_credit": rates.prospeo_per_credit,
            "notes": rates.notes,
        },
        "tier_order": order,
    }


def resolve_people(
    *,
    source_table: str,
    where: str = "",
    client_tag: str,
    max_tier: str = "all",
    min_tier: str = "",
    skip_tiers: str | list[str] | None = None,
    approve_cost_usd: float | None = None,
    estimate_only: bool = False,
    require_title_match: bool = True,
    write_supabase: bool = True,
    progress_callback: ProgressFn | None = None,
    vendors: VendorBundle | None = None,
) -> dict[str, Any]:
    tag = normalize_client_tag(client_tag)
    profile = get_profile(tag)
    src = parse_source(source_table, where, writeback=write_supabase)
    bundle = vendors or build_vendors()
    stored_free = profile.raw.get("leadmagic_search_free")
    if stored_free is None and isinstance(
        profile.people_measured_rates.get("leadmagic_search_free"), bool
    ):
        stored_free = profile.people_measured_rates.get("leadmagic_search_free")
    rates = read_live_rates(
        bundle,
        stored_search_free=stored_free if isinstance(stored_free, bool) else None,
    )
    order = compute_tier_order(
        rates=rates,
        measured_rates=profile.people_measured_rates,
        dropped_tiers=profile.people_dropped_tiers,
    )
    allowed = select_tiers(
        order,
        max_tier=max_tier,
        min_tier=min_tier,
        skip_tiers=skip_tiers,
    )
    skipped = parse_tier_list(skip_tiers)

    if estimate_only:
        quote = estimate_job(src, profile=profile, rates=rates, order=order, tiers=allowed)
        quote["min_tier"] = min_tier or ""
        quote["max_tier"] = max_tier
        quote["skip_tiers"] = skipped
        return quote

    if write_supabase:
        ensure_people_writeback(src)

    known = load_known_names(profile) if write_supabase else set()
    spent = 0.0
    total_rows = count_source(src)
    stats = {
        "companies": 0,
        "resolved": 0,
        "partial": 0,
        "deferred": 0,
        "people_unresolved": 0,
        "title_matched": 0,
        "name_bank": 0,
        "written": 0,
        "spent_usd": 0.0,
        "next_tier": None,
        "per_tier": {t: {"calls": 0, "people": 0, "title_matched": 0, "usd": 0.0} for t in allowed},
    }
    deferred = False
    next_tier = None

    def emit(phase: str = "running") -> None:
        snapshot_stats = {
            **stats,
            "companies_with_people": int(stats["resolved"]) + int(stats["partial"]),
            "companies_unresolved": int(stats["people_unresolved"]),
        }
        counter = counter_from_stats(snapshot_stats, total=total_rows, phase=phase)
        if progress_callback:
            progress_callback(
                {
                    "status": "deferred" if deferred else phase,
                    "client_tag": tag,
                    "input_rows": total_rows,
                    "progress": dict(snapshot_stats),
                    "counter": counter,
                    "spent_usd": round(spent, 4),
                    "next_tier": next_tier,
                }
            )

    emit("running")

    def _bill(
        tier: str,
        people: list[PersonHit],
        *,
        unit: float,
        billing: str,
        cost_override: float | None = None,
    ) -> float:
        nonlocal spent
        if cost_override is not None:
            cost = cost_override
        elif billing == "always":
            if tier == "serp":
                cost = unit
            elif tier == "leadmagic_employee":
                cost = unit * max(len(people), 0)
            elif tier == "aiark":
                cost = unit * len(people)
            else:
                cost = unit * max(len(people), 1 if people else 0)
        elif billing == "free_on_miss" and people:
            cost = unit * (1 if tier == "prospeo" else len(people))
            if getattr(bundle.prospeo, "last_free", False) and tier == "prospeo":
                cost = 0.0
        else:
            cost = 0.0
        spent += cost
        stats["per_tier"][tier]["usd"] += cost
        stats["spent_usd"] = spent
        return cost

    def _apply_people(
        work: _CompanyWork,
        tier: str,
        people: list[PersonHit],
        *,
        titles: list[str],
        fallback: bool,
    ) -> None:
        hits, bank = _audit_people(
            people,
            profile=profile,
            company_name=work.company,
            domain=work.domain,
            use_fallback=fallback,
        )
        for person in bank:
            if write_supabase:
                write_name_bank(
                    client_tag=tag,
                    domain=work.domain,
                    person=person,
                    source=tier,
                )
            work.bank_count += 1
            stats["name_bank"] += 1
        for person, audit, conf in hits:
            if is_known(known, work.domain, person):
                continue
            known.add(((work.domain or person.domain or ""), person.name_key))
            work.last_source = tier
            stats["per_tier"][tier]["title_matched"] += 1
            stats["title_matched"] += 1
            if require_title_match or audit.title_match:
                work.accepted_rows.append(
                    contact_payload(
                        person,
                        audit,
                        client_tag=tag,
                        company_name=work.company,
                        domain=work.domain,
                        source_tier=tier,
                        source_confidence=conf,
                    )
                )

    def _would_defer(tier: str, unit: float, billing: str, *, n: int = 1) -> bool:
        nonlocal deferred, next_tier
        if billing == "free" or approve_cost_usd is None:
            return False
        projected = spent + unit * max(1, n)
        if projected <= approve_cost_usd + 1e-9:
            return False
        deferred = True
        next_tier = tier
        stats["next_tier"] = tier
        return True

    def run_pass(work: _CompanyWork, tiers: list[str], titles: list[str], fallback: bool) -> None:
        for tier in tiers:
            if tier == "serp":
                continue
            meta = next((r for r in order if r["tier"] == tier), {})
            unit = float(meta.get("unit_usd") or 0)
            billing = meta.get("billing") or "always"
            if _would_defer(tier, unit, billing):
                work.deferred = True
                return
            people = _call_tier(
                tier,
                bundle,
                profile=profile,
                domain=work.domain,
                company_name=work.company,
                city=work.city,
                state=work.state,
                titles=titles,
                first_name=work.first,
                last_name=work.last,
            )
            stats["per_tier"][tier]["calls"] += 1
            stats["per_tier"][tier]["people"] += len(people)
            _bill(tier, people, unit=unit, billing=billing)
            _apply_people(work, tier, people, titles=titles, fallback=fallback)
            if work.accepted_rows or deferred:
                if deferred:
                    work.deferred = True
                return

    def run_serp_batch(works: list[_CompanyWork], titles: list[str], fallback: bool) -> None:
        nonlocal deferred, next_tier
        if not works or not bundle.serp.enabled:
            return
        meta = next((r for r in order if r["tier"] == "serp"), {})
        unit = float(meta.get("unit_usd") or 0)
        billing = meta.get("billing") or "always"
        runnable: list[_CompanyWork] = []
        jobs: list[SerpQuery] = []
        for work in works:
            query = build_query(work.company, titles)
            if not query:
                continue
            if _would_defer("serp", unit, billing):
                work.deferred = True
                for rest in works[works.index(work) + 1 :]:
                    rest.deferred = True
                break
            key = str(work.row.get("_source_key") or id(work))
            runnable.append(work)
            jobs.append(
                SerpQuery(
                    key=key,
                    query=query,
                    company_name=work.company,
                    titles=list(titles),
                )
            )
        if not jobs:
            return
        emit("serp")
        packed = bundle.serp.resolve_queries(jobs, profile=profile, unit=unit)
        by_key = {item.key: item for item in packed}
        for work, job in zip(runnable, jobs):
            item = by_key.get(job.key)
            people = list(item.people) if item else []
            cost = float(item.cost_usd) if item else 0.0
            stats["per_tier"]["serp"]["calls"] += 1
            stats["per_tier"]["serp"]["people"] += len(people)
            _bill("serp", people, unit=unit, billing=billing, cost_override=cost)
            _apply_people(work, "serp", people, titles=titles, fallback=fallback)

    def finalize(work: _CompanyWork) -> None:
        if work.row.get("_finalized"):
            return
        work.row["_finalized"] = True
        if work.accepted_rows and write_supabase:
            stats["written"] += write_contacts(profile, work.accepted_rows)
        if work.deferred:
            status = "deferred"
            stats["deferred"] += 1
        elif work.accepted_rows:
            status = "resolved"
            stats["resolved"] += 1
        elif work.bank_count:
            status = "partial"
            stats["partial"] += 1
        else:
            status = "people_unresolved"
            stats["people_unresolved"] += 1
        if write_supabase:
            writeback_people(
                src,
                work.row.get("_source_key"),
                count=len(work.accepted_rows),
                source=work.last_source or ("fallback" if work.used_fallback else ""),
                status=status,
            )
        emit()

    def finalize_rest(works: list[_CompanyWork], *, as_deferred: bool = False) -> None:
        for work in works:
            if work.row.get("_finalized"):
                continue
            if as_deferred and not work.accepted_rows:
                work.deferred = True
            finalize(work)

    def finish_after_serp(works: list[_CompanyWork], titles: list[str], fallback: bool) -> None:
        for i, work in enumerate(works):
            if work.accepted_rows or work.deferred:
                finalize(work)
                continue
            if work.after and not deferred:
                run_pass(work, work.after, titles, fallback)
            finalize(work)
            if deferred:
                finalize_rest(works[i + 1 :], as_deferred=True)
                break

    pending_serp: list[_CompanyWork] = []
    pending_after: list[_CompanyWork] = []

    for row in iter_source(src):
        stats["companies"] += 1
        domain = str(row.get("domain") or "").strip().lower()
        company = str(row.get("company_name") or "").strip()
        city = str(row.get("city") or "").strip()
        state = str(row.get("state") or "").strip()
        first = str(row.get("first_name") or "").strip()
        last = str(row.get("last_name") or "").strip()
        lane = "domain" if domain else "name"
        lane_order = _lane_order(allowed, order, lane, company)
        before, has_serp, after = _split_lane(lane_order)
        work = _CompanyWork(
            row=row,
            domain=domain,
            company=company,
            city=city,
            state=state,
            first=first,
            last=last,
            lane_order=lane_order,
            before=before,
            has_serp=has_serp,
            after=after,
        )
        run_pass(work, work.before, list(profile.target_titles), False)
        if work.accepted_rows or work.deferred:
            finalize(work)
            if deferred:
                break
            continue
        if work.has_serp:
            pending_serp.append(work)
        elif work.after:
            pending_after.append(work)
        elif profile.fallback_titles:
            work.used_fallback = True
            run_pass(work, work.before, list(profile.fallback_titles), True)
            if work.accepted_rows or work.deferred:
                finalize(work)
            elif work.has_serp:
                pending_serp.append(work)
            else:
                finalize(work)
            if deferred:
                break
        else:
            finalize(work)
        emit()
        if deferred:
            break

    if pending_serp:
        run_serp_batch(pending_serp, list(profile.target_titles), False)
        still: list[_CompanyWork] = []
        for work in pending_serp:
            if work.accepted_rows or work.deferred:
                finalize(work)
            else:
                still.append(work)
        fallback_serp: list[_CompanyWork] = []
        for i, work in enumerate(still):
            if work.after:
                run_pass(work, work.after, list(profile.target_titles), False)
            if work.accepted_rows or work.deferred:
                finalize(work)
                if deferred:
                    finalize_rest(still[i + 1 :], as_deferred=True)
                    fallback_serp = []
                    break
                continue
            if profile.fallback_titles:
                work.used_fallback = True
                run_pass(work, work.before, list(profile.fallback_titles), True)
                if work.accepted_rows or work.deferred:
                    finalize(work)
                elif work.has_serp:
                    fallback_serp.append(work)
                else:
                    if work.after:
                        run_pass(work, work.after, list(profile.fallback_titles), True)
                    finalize(work)
                if deferred:
                    finalize_rest(still[i + 1 :], as_deferred=True)
                    fallback_serp = []
                    break
            else:
                finalize(work)
        if fallback_serp and not deferred:
            run_serp_batch(fallback_serp, list(profile.fallback_titles), True)
            finish_after_serp(fallback_serp, list(profile.fallback_titles), True)
        elif fallback_serp:
            finalize_rest(fallback_serp, as_deferred=True)

    if pending_after and not deferred:
        for i, work in enumerate(pending_after):
            run_pass(work, work.after, list(profile.target_titles), False)
            if not work.accepted_rows and profile.fallback_titles and not deferred:
                work.used_fallback = True
                run_pass(work, work.before + work.after, list(profile.fallback_titles), True)
            finalize(work)
            if deferred:
                finalize_rest(pending_after[i + 1 :], as_deferred=True)
                break
    elif pending_after:
        finalize_rest(pending_after, as_deferred=True)

    handoff: dict[str, Any] = {}
    if write_supabase and stats["title_matched"] and not estimate_only:
        handoff = handoff_title_matches(profile)

    result = {
        "ok": True,
        "client_tag": tag,
        "source_table": src.qualified,
        "status": "deferred" if deferred else "completed",
        "input_rows": total_rows,
        "counter": counter_from_stats(
            {
                **stats,
                "companies_with_people": int(stats["resolved"]) + int(stats["partial"]),
                "companies_unresolved": int(stats["people_unresolved"]),
            },
            total=total_rows,
            phase="deferred" if deferred else "completed",
        ),
        "counts": {
            "companies": stats["companies"],
            "resolved": stats["resolved"],
            "partial": stats["partial"],
            "deferred": stats["deferred"],
            "people_unresolved": stats["people_unresolved"],
            "title_matched": stats["title_matched"],
            "name_bank": stats["name_bank"],
            "written": stats["written"],
        },
        "spent_usd": round(spent, 4),
        "next_tier": next_tier,
        "min_tier": min_tier or "",
        "max_tier": max_tier,
        "skip_tiers": skipped,
        "selected_tiers": list(allowed),
        "per_tier": stats["per_tier"],
        "serp_actor_runs": int(getattr(bundle.serp, "runs", 0) or 0),
        "tier_order": order,
        "live_rates": {
            "leadmagic_per_credit": rates.leadmagic_per_credit,
            "leadmagic_credits": rates.leadmagic_credits,
            "leadmagic_plan": rates.leadmagic_plan,
            "leadmagic_search_free": rates.leadmagic_search_free,
            "aiark_per_credit": rates.aiark_per_credit,
            "notes": rates.notes,
        },
        "handoff": handoff,
    }
    if progress_callback:
        progress_callback({**result, "status": result["status"]})
    return result
