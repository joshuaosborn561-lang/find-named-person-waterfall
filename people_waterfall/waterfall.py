"""Resolve named people. Never finds an email. Profile-driven, cheapest first."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .gate import gate_person
from .handoff import handoff_title_matches
from .people import PersonHit, conversational_company, looks_like_person
from .pricing import (
    PUBLISHED,
    LiveRates,
    compute_tier_order,
    include_from_profile,
    parse_tier_list,
    select_tiers,
)
from .profile import (
    ClientProfile,
    build_discolike_icp,
    get_profile,
    merge_people_measured_rates,
    normalize_client_tag,
    persist_discolike_icp,
)
from .progress import counter_from_stats
from .source import (
    TableSource,
    count_source,
    count_source_with_domain,
    ensure_people_writeback,
    iter_source,
    parse_source,
    writeback_people,
)
from .preflight import preflight_job
from .site_quality import (
    bankable_person,
    name_is_valid,
    should_pause_for_site_staff_quality,
)
from .vendors.cache import CacheClient
from .vendors.discolike import DiscoLikeClient
from .vendors.leadmagic import LeadMagicClient
from .vendors.site_staff import SiteStaffClient
from .vendors.leadmagic import per_credit_from_payload as lm_per_credit
from .write import (
    call_with_retry,
    contact_payload,
    dedupe_person_rows,
    is_known,
    load_known_names,
    name_bank_row,
    write_contacts,
    write_name_bank_rows,
)

ProgressFn = Callable[[dict[str, Any]], None]
FLUSH_EVERY = 50
SPEND_PAUSE_AFTER = 100


def should_pause_for_spend(companies_done: int, spent_usd: float, contacts_written: int) -> bool:
    """Stop a paid run that is spending without writing contacts."""
    return (
        int(companies_done) >= SPEND_PAUSE_AFTER
        and float(spent_usd) > 0
        and int(contacts_written) <= 0
    )


@dataclass
class VendorBundle:
    cache: CacheClient
    leadmagic: LeadMagicClient
    discolike: DiscoLikeClient
    site_staff: SiteStaffClient | None = None

    def __post_init__(self) -> None:
        if self.site_staff is None:
            self.site_staff = SiteStaffClient(enabled=False)

    def for_tier(self, tier: str) -> Any:
        return {
            "cache": self.cache,
            "leadmagic_employee": self.leadmagic,
            "discolike": self.discolike,
            "site_staff": self.site_staff,
        }.get(tier)


def build_vendors() -> VendorBundle:
    return VendorBundle(
        cache=CacheClient(),
        leadmagic=LeadMagicClient(),
        discolike=DiscoLikeClient(),
        site_staff=SiteStaffClient(),
    )


def read_live_rates(vendors: VendorBundle) -> LiveRates:
    rates = LiveRates()
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
    return client.find_people(**kwargs)


def _lane_order(
    allowed: list[str],
    order: list[dict[str, Any]],
    lane: str,
) -> list[str]:
    """min_tier / max_tier can name a tier that is not in the default order.
    Use PUBLISHED.needs so those windows still land on the right lane.
    """
    known = {row["tier"]: row for row in order}
    out: list[str] = []
    for tier in allowed:
        meta = known.get(tier) or PUBLISHED.get(tier) or {}
        needs = meta.get("needs")
        if lane == "domain" and needs in {"domain", "either"}:
            out.append(tier)
        elif lane == "name" and needs in {"name", "either"}:
            out.append(tier)
    return out


def _meta_for(order: list[dict[str, Any]], tier: str, rates: LiveRates) -> dict[str, Any]:
    row = next((r for r in order if r["tier"] == tier), None)
    if row:
        return row
    meta = PUBLISHED.get(tier) or {}
    return {
        "tier": tier,
        "unit_usd": rates.unit_usd(tier),
        "billing": meta.get("billing") or "always",
        "needs": meta.get("needs"),
    }


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
    after: list[str]
    accepted_rows: list[dict[str, Any]] = field(default_factory=list)
    bank_people: list[PersonHit] = field(default_factory=list)
    seen_people: list[PersonHit] = field(default_factory=list)
    tiers_called: set[str] = field(default_factory=set)
    handled_keys: set[str] = field(default_factory=set)
    persisted_contacts: int = 0
    persisted_bank: int = 0
    finished: bool = False
    bank_count: int = 0
    last_source: str = ""
    used_fallback: bool = False
    deferred: bool = False
    people_found: bool = False
    reason: str = ""
    email_pattern: str = ""
    email_pattern_confidence: float | None = None


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
        decision = gate_person(
            person,
            profile,
            company_name=company_name,
            domain=domain,
            use_fallback=use_fallback,
        )
        if decision.audit is not None and decision.audit.title_rank is not None:
            person.title_rank = decision.audit.title_rank
        if decision.reason:
            person.rejection_reason = decision.reason
            bank.append(person)
            continue
        accepted.append((person, decision.audit, decision.confidence))
    return accepted, bank


def _unique_people(people: list[PersonHit]) -> list[PersonHit]:
    """One hit per normalized name. The first occurrence wins."""
    seen: set[str] = set()
    out: list[PersonHit] = []
    for person in people:
        key = (person.name_key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(person)
    return out


def adopt_sibling(primary: _CompanyWork, sibling: _CompanyWork) -> None:
    """Copy a domain's gated result onto another source row for writeback only.

    persisted_* is set to the copied length so a later flush does not bank or
    insert the same (domain, person) again.
    """
    sibling.accepted_rows = list(primary.accepted_rows)
    sibling.bank_people = list(primary.bank_people)
    sibling.email_pattern = primary.email_pattern
    sibling.email_pattern_confidence = primary.email_pattern_confidence
    sibling.last_source = primary.last_source
    sibling.reason = primary.reason or sibling.reason
    sibling.bank_count = primary.bank_count
    sibling.deferred = primary.deferred
    sibling.people_found = primary.people_found
    sibling.used_fallback = primary.used_fallback
    sibling.tiers_called.update(primary.tiers_called)
    sibling.handled_keys.update(primary.handled_keys)
    sibling.seen_people = list(primary.seen_people)
    sibling.persisted_contacts = len(sibling.accepted_rows)
    sibling.persisted_bank = len(sibling.bank_people)


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
    by_name = {row["tier"]: row for row in order}
    for name in tiers:
        row = by_name.get(name)
        if row is None:
            meta = PUBLISHED.get(name) or {}
            row = {
                "tier": name,
                "unit_usd": rates.unit_usd(name),
                "billing": meta.get("billing"),
                "needs": meta.get("needs"),
                "measured_rate": None,
            }
        unit = float(row.get("unit_usd") or 0)
        billing = row.get("billing")
        priced_rows = rows
        if billing == "free":
            cost = 0.0
        elif billing == "free_on_miss":
            rate = row.get("measured_rate")
            cost = unit * rows * (rate if rate is not None else 0.5)
        elif row["tier"] == "discolike":
            priced_rows = count_source_with_domain(src)
            cost = unit * priced_rows
        else:
            cost = unit * rows
        total += cost
        per_tier.append(
            {
                "tier": name,
                "rows": priced_rows,
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
    on_discolike_task: Callable[[str], None] | None = None,
    record_measured_rates: bool = True,
    handoff: bool = True,
) -> dict[str, Any]:
    tag = normalize_client_tag(client_tag)
    profile = get_profile(tag)
    src = parse_source(source_table, where, writeback=write_supabase)
    src.client_tag = tag
    if write_supabase and not estimate_only:
        preflight_job(
            client_tag=tag,
            source_table=source_table,
            where=where,
            alter=True,
            profile=profile,
            src=src,
        )
    bundle = vendors or build_vendors()
    if on_discolike_task is not None:
        bundle.discolike.on_task_started = on_discolike_task
    rates = read_live_rates(bundle)
    order = compute_tier_order(
        rates=rates,
        measured_rates=profile.people_measured_rates,
        dropped_tiers=profile.people_dropped_tiers,
        include=include_from_profile(profile.people_tier_order),
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
        "companies_done": 0,
        "row_failures": 0,
        "pause_reason": "",
        "site_staff": {
            "domains": 0,
            "title_matched": 0,
            "personal": 0,
            "role": 0,
            "generic": 0,
            "banked_no_email": 0,
            "quality_domains": 0,
            "quality_written": 0,
            "quality_valid": 0,
        },
        "per_tier": {t: {"calls": 0, "people": 0, "title_matched": 0, "usd": 0.0} for t in allowed},
    }
    deferred = False
    pause_reason = ""
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
                    "contacts_written": int(stats["written"]),
                    "title_matched": int(stats["title_matched"]),
                    "companies_done": int(stats["companies_done"]),
                    "next_tier": next_tier,
                    "pause_reason": pause_reason,
                }
            )

    emit("running")

    def _tier_stat(key: str) -> dict[str, Any]:
        return stats["per_tier"].setdefault(
            key, {"calls": 0, "people": 0, "title_matched": 0, "usd": 0.0}
        )

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
            if tier == "leadmagic_employee":
                cost = unit * max(len(people), 0)
            else:
                cost = unit * max(len(people), 1 if people else 0)
        elif billing == "free_on_miss" and people:
            cost = unit * len(people)
        else:
            cost = 0.0
        spent += cost
        _tier_stat(tier)["usd"] += cost
        stats["spent_usd"] = spent
        return cost

    def _apply_people(
        work: _CompanyWork,
        tier: str,
        people: list[PersonHit],
        *,
        titles: list[str],
        fallback: bool,
        source: str | None = None,
    ) -> None:
        src_name = source or tier
        hits, bank = _audit_people(
            people,
            profile=profile,
            company_name=work.company,
            domain=work.domain,
            use_fallback=fallback,
        )
        for person in bank:
            key = person.name_key or f"{person.first_name}|{person.last_name}"
            if key in work.handled_keys:
                continue
            work.handled_keys.add(key)
            work.bank_people.append(person)
            work.bank_count += 1
            stats["name_bank"] += 1
        for person, audit, conf in hits:
            try:
                if not (person.last_name or "").strip():
                    person.last_name = ""
                    person.rejection_reason = "single_name"
                    key = person.name_key or person.first_name
                    if key in work.handled_keys:
                        continue
                    work.handled_keys.add(key)
                    work.bank_people.append(person)
                    work.bank_count += 1
                    stats["name_bank"] += 1
                    continue
            except Exception:
                stats["row_failures"] += 1
                continue
            key = person.name_key or f"{person.first_name}|{person.last_name}"
            if key in work.handled_keys and not any(
                (row.get("first_name"), row.get("last_name"))
                == (person.first_name, person.last_name)
                for row in work.accepted_rows
            ):
                # Fallback titles can promote a previously banked name.
                work.handled_keys.discard(key)
            if key in work.handled_keys:
                continue
            if is_known(known, work.domain, person):
                work.handled_keys.add(key)
                continue
            work.handled_keys.add(key)
            known.add(((work.domain or person.domain or ""), person.name_key))
            work.last_source = src_name
            _tier_stat(tier)["title_matched"] += 1
            if src_name != tier:
                _tier_stat(src_name)["title_matched"] += 1
            stats["title_matched"] += 1
            if require_title_match or audit.title_match:
                work.accepted_rows.append(
                    contact_payload(
                        person,
                        audit,
                        client_tag=tag,
                        company_name=work.company,
                        domain=work.domain,
                        source_tier=src_name,
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

    def _company_status(work: _CompanyWork) -> str:
        if work.deferred:
            return "deferred"
        if work.accepted_rows:
            return "resolved"
        if work.bank_count:
            return "partial"
        return "people_unresolved"

    def _flush(works: list[_CompanyWork], *, finished: bool) -> None:
        """Persist this tier batch before the next tier spends."""
        bank_rows: list[dict[str, Any]] = []
        contact_rows: list[dict[str, Any]] = []
        for work in works:
            fresh_bank = work.bank_people[work.persisted_bank :]
            work.persisted_bank = len(work.bank_people)
            for person in fresh_bank:
                row = name_bank_row(
                    client_tag=tag,
                    domain=work.domain,
                    person=person,
                    source=work.last_source or person.source_tier or "discolike",
                )
                row["title_rank"] = person.title_rank
                bank_rows.append(row)
            fresh_contacts = work.accepted_rows[work.persisted_contacts :]
            work.persisted_contacts = len(work.accepted_rows)
            contact_rows.extend(fresh_contacts)
        contact_rows = dedupe_person_rows(contact_rows)
        bank_rows = dedupe_person_rows(bank_rows)
        for row in bank_rows:
            row.pop("title_rank", None)
        # Contacts first, then the bank, then source status. A contacts failure
        # stops the batch before name_bank or writeback.
        if write_supabase and contact_rows:
            stats["written"] += write_contacts(profile, contact_rows)
        if write_supabase and bank_rows:
            write_name_bank_rows(bank_rows)
        if write_supabase:
            for work in works:
                call_with_retry(
                    lambda work=work: writeback_people(
                        src,
                        work.row.get("_source_key"),
                        count=len(work.accepted_rows),
                        source=work.last_source or ("fallback" if work.used_fallback else ""),
                        status=_company_status(work),
                        reason=work.reason,
                        email_pattern=work.email_pattern,
                        email_pattern_confidence=work.email_pattern_confidence,
                    )
                )
        if not finished:
            emit()
            return
        for work in works:
            if work.finished:
                continue
            work.finished = True
            work.row["_finalized"] = True
            stats["companies"] += 1
            status = _company_status(work)
            if status == "deferred":
                stats["deferred"] += 1
            elif status == "resolved":
                stats["resolved"] += 1
            elif status == "partial":
                stats["partial"] += 1
            else:
                stats["people_unresolved"] += 1
        emit()

    def _finish(work: _CompanyWork) -> None:
        _flush([work], finished=True)

    def run_pass(work: _CompanyWork, tiers: list[str], titles: list[str], fallback: bool) -> None:
        for tier in tiers:
            if tier in {"discolike", "site_staff"}:
                continue
            if work.accepted_rows:
                return
            if tier in work.tiers_called:
                continue
            meta = _meta_for(order, tier, rates)
            unit = float(meta.get("unit_usd") or 0)
            billing = meta.get("billing") or "always"
            if billing != "free":
                client = bundle.for_tier(tier)
                if client is None or not getattr(client, "enabled", True):
                    raise RuntimeError(
                        f"{tier} is selected but is not enabled; refusing to fall through"
                    )
            if _would_defer(tier, unit, billing):
                work.deferred = True
                return
            people = _unique_people(
                _call_tier(
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
            )
            work.tiers_called.add(tier)
            work.last_source = tier
            work.seen_people.extend(people)
            stats["per_tier"][tier]["calls"] += 1
            stats["per_tier"][tier]["people"] += len(people)
            _bill(tier, people, unit=unit, billing=billing)
            _apply_people(work, tier, people, titles=titles, fallback=fallback)
            # Persist this company before the next paid call.
            _flush([work], finished=False)
            if work.accepted_rows or deferred:
                if deferred:
                    work.deferred = True
                return

    def run_discolike_batch(works: list[_CompanyWork]) -> None:
        if not works:
            return
        meta = _meta_for(order, "discolike", rates)
        unit = float(meta.get("unit_usd") or 0)
        billing = meta.get("billing") or "always"
        icp = build_discolike_icp(profile)
        if write_supabase and not profile.discolike_icp_text:
            persist_discolike_icp(tag, icp)
            profile.discolike_icp_text = icp
        queued: list[_CompanyWork] = []
        for work in works:
            if work.accepted_rows:
                continue
            if not work.domain:
                work.reason = "no_domain"
                continue
            if "discolike" in work.tiers_called:
                continue
            if _would_defer("discolike", unit, billing):
                work.deferred = True
                for rest in works[works.index(work) + 1 :]:
                    if not rest.accepted_rows:
                        rest.deferred = True
                break
            queued.append(work)
        if not queued:
            return
        if not bundle.discolike.enabled:
            raise RuntimeError(
                "discolike is selected but DISCOLIKE_API_KEY is missing; refusing to fall through"
            )
        emit("discolike")
        calls_before = int(getattr(bundle.discolike, "calls", 0) or 0)
        companies = {work.domain: work.company for work in queued if work.domain}
        packed = bundle.discolike.resolve_domains(
            [work.domain for work in queued],
            profile=profile,
            companies=companies,
            unit=unit,
            icp_text=icp,
        )
        calls_made = int(getattr(bundle.discolike, "calls", 0) or 0) - calls_before
        if calls_made <= 0:
            raise RuntimeError(
                getattr(bundle.discolike, "last_error", "")
                or "discolike was selected but made 0 calls; refusing to fall through"
            )
        by_domain: dict[str, list[_CompanyWork]] = {}
        for work in queued:
            by_domain.setdefault(work.domain, []).append(work)
        for domain, group in by_domain.items():
            row = packed.get(domain)
            people = _unique_people(list(row.people) if row else [])
            people_keys = {person.name_key for person in people}
            bank_only = [
                person
                for person in _unique_people(list(row.bank_only) if row else [])
                if person.name_key not in people_keys
            ]
            cost = float(row.cost_usd) if row else 0.0
            _tier_stat("discolike")["calls"] += 1
            _tier_stat("discolike")["people"] += len(people) + len(bank_only)
            _bill("discolike", people, unit=unit, billing=billing, cost_override=cost)
            primary = group[0]
            primary.tiers_called.add("discolike")
            primary.last_source = "discolike"
            if row and row.email_pattern:
                primary.email_pattern = row.email_pattern
                primary.email_pattern_confidence = row.email_pattern_confidence
            primary.seen_people.extend(people)
            primary.seen_people.extend(bank_only)
            _apply_people(
                primary,
                "discolike",
                [*people, *bank_only],
                titles=target_titles,
                fallback=False,
                source="discolike",
            )
            for sibling in group[1:]:
                adopt_sibling(primary, sibling)
                sibling.tiers_called.add("discolike")
                sibling.last_source = "discolike"
        # Write the whole DiscoLike batch before any later tier spends.
        _flush(queued, finished=False)

    inflight: dict[str, _CompanyWork] = {}
    waiting: dict[str, list[_CompanyWork]] = {}

    def _note(n: int = 1) -> bool:
        nonlocal deferred, pause_reason
        stats["companies_done"] = int(stats["companies_done"]) + max(0, n)
        if should_pause_for_spend(
            int(stats["companies_done"]), spent, int(stats["written"])
        ):
            deferred = True
            pause_reason = "spending_without_output"
            stats["pause_reason"] = pause_reason
            emit("paused")
            return True
        emit()
        return False

    def _check_site_staff_quality(block: dict[str, Any]) -> bool:
        nonlocal deferred, pause_reason
        block["quality_domains"] = int(block.get("quality_domains") or 0) + 1
        if not should_pause_for_site_staff_quality(
            int(block["quality_domains"]),
            int(block.get("quality_valid") or 0),
            int(block.get("quality_written") or 0),
        ):
            return False
        deferred = True
        pause_reason = "site_staff_quality"
        stats["pause_reason"] = pause_reason
        emit("paused")
        return True

    def run_site_staff_batch(works: list[_CompanyWork]) -> None:
        targets = [
            work
            for work in works
            if work.domain and "site_staff" not in work.tiers_called and not work.accepted_rows
        ]
        if not targets or "site_staff" not in allowed:
            return
        client = bundle.site_staff
        if client is None or not getattr(client, "enabled", True):
            return
        emit("site_staff")
        companies = {work.domain: work.company for work in targets if work.domain}
        packed = client.resolve_domains(
            [work.domain for work in targets],
            profile=profile,
            companies=companies,
        )
        by_domain: dict[str, list[_CompanyWork]] = {}
        for work in targets:
            by_domain.setdefault(work.domain, []).append(work)
        block = stats["site_staff"]
        for domain, group in by_domain.items():
            try:
                result = packed.get(domain)
                primary = group[0]
                primary.tiers_called.add("site_staff")
                block["domains"] += 1
                _tier_stat("site_staff")["calls"] += 1
                if result and result.generic_emails:
                    primary.email_pattern = ",".join(result.generic_emails[:5])
                    primary.email_pattern_confidence = 1.0
                    block["generic"] += len(result.generic_emails)
                person = result.person if result else None
                if person is None:
                    for sibling in group[1:]:
                        sibling.tiers_called.add("site_staff")
                    _check_site_staff_quality(block)
                    continue
                if not bankable_person(person.first_name, person.last_name, person.title):
                    for sibling in group[1:]:
                        sibling.tiers_called.add("site_staff")
                    _check_site_staff_quality(block)
                    continue
                _tier_stat("site_staff")["people"] += 1
                _bill("site_staff", [person], unit=0.0, billing="free")
                if result.bank_only:
                    person.name_bank_status = "needs_email"
                    person.rejection_reason = ""
                    person.domain = domain
                    person.company_name = primary.company
                    person.source_tier = "site_staff"
                    key = person.name_key or person.first_name
                    if key not in primary.handled_keys:
                        primary.handled_keys.add(key)
                        primary.bank_people.append(person)
                        primary.bank_count += 1
                        stats["name_bank"] += 1
                    primary.last_source = "site_staff"
                    primary.people_found = True
                    block["title_matched"] += 1
                    block["banked_no_email"] += 1
                else:
                    before = len(primary.accepted_rows)
                    _apply_people(
                        primary,
                        "site_staff",
                        [person],
                        titles=list(profile.target_titles),
                        fallback=False,
                        source="site_staff",
                    )
                    if primary.accepted_rows:
                        primary.people_found = True
                        primary.last_source = "site_staff"
                        block["title_matched"] += 1
                        kind = result.email_type or "personal"
                        if kind in {"personal", "role"}:
                            block[kind] += 1
                        for row in primary.accepted_rows[before:]:
                            block["quality_written"] += 1
                            if name_is_valid(
                                str(row.get("first_name") or ""),
                                str(row.get("last_name") or ""),
                            ):
                                block["quality_valid"] += 1
                for sibling in group[1:]:
                    adopt_sibling(primary, sibling)
                    sibling.tiers_called.add("site_staff")
                    sibling.people_found = primary.people_found
            except Exception:
                stats["row_failures"] += 1
                continue
            if _check_site_staff_quality(block):
                break
        _flush(targets, finished=False)

    def finalize(work: _CompanyWork) -> None:
        if work.finished:
            return
        _finish(work)
        if not work.domain or inflight.get(work.domain) is not work:
            return
        for sibling in waiting.pop(work.domain, []):
            adopt_sibling(work, sibling)
            finalize(sibling)

    def finalize_rest(works: list[_CompanyWork], *, as_deferred: bool = False) -> None:
        for work in works:
            if work.finished:
                continue
            if as_deferred and not work.accepted_rows:
                work.deferred = True
            finalize(work)

    pending_disco: list[_CompanyWork] = []
    pending_after: list[_CompanyWork] = []
    site_buf: list[_CompanyWork] = []
    target_titles = list(profile.target_titles)
    fallback_titles = list(profile.fallback_titles)

    def intake_paid(work: _CompanyWork) -> None:
        if work.people_found or work.finished:
            if not work.finished:
                finalize(work)
            return
        run_pass(work, work.before, target_titles, False)
        if work.accepted_rows or work.deferred:
            finalize(work)
            return
        if has_disco_for(work):
            if not work.domain:
                work.reason = "no_domain"
                if work.after and not work.accepted_rows:
                    pending_after.append(work)
                    return
                finalize(work)
                return
            pending_disco.append(work)
            return
        if work.after and not work.accepted_rows:
            pending_after.append(work)
            return
        if fallback_titles and work.seen_people and not work.accepted_rows:
            work.used_fallback = True
            _apply_people(
                work,
                work.last_source or "cache",
                work.seen_people,
                titles=fallback_titles,
                fallback=True,
            )
        if not work.domain and not work.accepted_rows:
            work.reason = work.reason or "no_domain"
        finalize(work)

    def has_disco_for(work: _CompanyWork) -> bool:
        return "discolike" in work.lane_order

    def drain_site(force: bool = False) -> None:
        if not site_buf:
            return
        if not force and len(site_buf) < FLUSH_EVERY:
            return
        batch = list(site_buf)
        site_buf.clear()
        run_site_staff_batch(batch)
        if _note(len(batch)):
            for work in batch:
                if not work.finished:
                    finalize(work)
            return
        for work in batch:
            if deferred:
                if not work.finished:
                    work.deferred = True
                    finalize(work)
                continue
            intake_paid(work)

    for row in iter_source(src):
        domain = str(row.get("domain") or "").strip().lower()
        company = conversational_company(row) or str(row.get("company_name") or "").strip()
        city = str(row.get("city") or "").strip()
        state = str(row.get("state") or "").strip()
        first = str(row.get("first_name") or "").strip()
        last = str(row.get("last_name") or "").strip()
        lane = "domain" if domain else "name"
        lane_order = _lane_order(allowed, order, lane)
        if "discolike" in lane_order:
            idx = lane_order.index("discolike")
            before, rest = lane_order[:idx], lane_order[idx:]
        else:
            before, rest = list(lane_order), []
        has_disco = "discolike" in rest or "discolike" in lane_order
        after = [t for t in rest if t != "discolike"]
        before = [tier for tier in before if tier != "site_staff"]
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
            after=after,
        )
        if domain and domain in inflight:
            owner = inflight[domain]
            if owner.finished:
                adopt_sibling(owner, work)
                finalize(work)
            else:
                waiting.setdefault(domain, []).append(work)
            if deferred:
                break
            continue
        if domain:
            inflight[domain] = work
        if "site_staff" in work.lane_order and domain:
            site_buf.append(work)
            drain_site(False)
            if deferred:
                break
            continue
        intake_paid(work)
        if _note(1):
            break
        if deferred:
            break

    drain_site(True)

    if pending_disco and not deferred:
        emit("discolike")
        meta = _meta_for(order, "discolike", rates)
        unit = float(meta.get("unit_usd") or 0)
        billing = meta.get("billing") or "always"
        index = 0
        handed_off: set[int] = set()
        while index < len(pending_disco) and not deferred:
            slice_works = pending_disco[index : index + FLUSH_EVERY]
            billable = [
                work
                for work in slice_works
                if work.domain
                and not work.accepted_rows
                and "discolike" not in work.tiers_called
            ]
            if billable and _would_defer("discolike", unit, billing, n=len(billable)):
                finalize_rest(pending_disco[index:], as_deferred=True)
                break
            run_discolike_batch(slice_works)
            for work in slice_works:
                if work.finished:
                    continue
                if work.accepted_rows or work.deferred:
                    finalize(work)
                    if deferred:
                        break
                    continue
                if work.after:
                    pending_after.append(work)
                    handed_off.add(id(work))
                    continue
                if fallback_titles and work.seen_people:
                    work.used_fallback = True
                    _apply_people(
                        work,
                        work.last_source or "discolike",
                        work.seen_people,
                        titles=fallback_titles,
                        fallback=True,
                    )
                finalize(work)
                if deferred:
                    break
            if should_pause_for_spend(
                int(stats["companies_done"]), spent, int(stats["written"])
            ):
                deferred = True
                pause_reason = "spending_without_output"
                stats["pause_reason"] = pause_reason
                finalize_rest(pending_disco[index + FLUSH_EVERY :], as_deferred=True)
                break
            index += FLUSH_EVERY
        if deferred:
            finalize_rest(
                [w for w in pending_disco if not w.finished and id(w) not in handed_off],
                as_deferred=True,
            )
    elif pending_disco:
        finalize_rest(pending_disco, as_deferred=True)

    if pending_after and not deferred:
        for i, work in enumerate(pending_after):
            if work.accepted_rows:
                finalize(work)
                continue
            run_pass(work, work.after, list(profile.target_titles), False)
            if not work.accepted_rows and profile.fallback_titles and work.seen_people and not deferred:
                work.used_fallback = True
                _apply_people(
                    work,
                    work.last_source or (work.after[-1] if work.after else "cache"),
                    work.seen_people,
                    titles=list(profile.fallback_titles),
                    fallback=True,
                )
            finalize(work)
            if deferred:
                finalize_rest(pending_after[i + 1 :], as_deferred=True)
                break
    elif pending_after:
        finalize_rest(pending_after, as_deferred=True)

    if write_supabase and not deferred and spent > 0 and int(stats["written"]) <= 0:
        raise RuntimeError(
            f"spent ${spent:.4f} but wrote 0 contacts; refusing to mark the job completed"
        )
    if not deferred and int(stats["companies"]) < int(total_rows):
        raise RuntimeError(
            f"finished {stats['companies']} of {total_rows} companies; refusing to mark the job completed"
        )

    job_status = "paused" if pause_reason else ("deferred" if deferred else "completed")
    handoff_result: dict[str, Any] = {}
    if (
        handoff
        and write_supabase
        and stats["title_matched"]
        and not estimate_only
        and not pause_reason
    ):
        handoff_result = handoff_title_matches(profile)

    result = {
        "ok": True,
        "client_tag": tag,
        "source_table": src.qualified,
        "status": job_status,
        "pause_reason": pause_reason,
        "input_rows": total_rows,
        "counter": counter_from_stats(
            {
                **stats,
                "companies_with_people": int(stats["resolved"]) + int(stats["partial"]),
                "companies_unresolved": int(stats["people_unresolved"]),
            },
            total=total_rows,
            phase=job_status if job_status != "paused" else "deferred",
        ),
        "counts": {
            "companies": stats["companies"],
            "companies_done": stats["companies_done"],
            "resolved": stats["resolved"],
            "partial": stats["partial"],
            "deferred": stats["deferred"],
            "people_unresolved": stats["people_unresolved"],
            "title_matched": stats["title_matched"],
            "name_bank": stats["name_bank"],
            "written": stats["written"],
            "contacts_written": stats["written"],
            "row_failures": stats["row_failures"],
        },
        "site_staff": dict(stats["site_staff"]),
        "spent_usd": round(spent, 4),
        "contacts_written": int(stats["written"]),
        "title_matched": int(stats["title_matched"]),
        "companies_done": int(stats["companies_done"]),
        "next_tier": next_tier,
        "min_tier": min_tier or "",
        "max_tier": max_tier,
        "skip_tiers": skipped,
        "selected_tiers": list(allowed),
        "per_tier": stats["per_tier"],
        "tier_order": order,
        "live_rates": {
            "leadmagic_per_credit": rates.leadmagic_per_credit,
            "leadmagic_credits": rates.leadmagic_credits,
            "leadmagic_plan": rates.leadmagic_plan,
            "notes": rates.notes,
        },
        "handoff": handoff_result,
    }
    if (
        write_supabase
        and record_measured_rates
        and "discolike" in stats["per_tier"]
        and not estimate_only
    ):
        block = stats["per_tier"]["discolike"]
        calls = int(block.get("calls") or 0)
        try:
            merge_people_measured_rates(
                tag,
                {
                    "discolike": {
                        "title_matched": int(block.get("title_matched") or 0),
                        "people": int(block.get("people") or 0),
                        "companies": calls,
                        "usd": round(float(block.get("usd") or 0), 4),
                        "title_match_rate": round(
                            int(block.get("title_matched") or 0) / calls, 4
                        )
                        if calls
                        else 0.0,
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            rates.notes.append(f"measured_rates write failed: {type(exc).__name__}")
    if progress_callback:
        progress_callback({**result, "status": result["status"]})
    return result


def resume_discolike_task(
    task_id: str,
    client_tag: str,
    source_table: str,
    where: str = "",
) -> dict[str, Any]:
    """Re-read a finished DiscoLike task and run gates and writes.

    GET /discogen/status/{task_id} is free. Nothing is billed again.
    """
    from .vendors.discolike import DiscoDomainResult, DiscoLikeClient

    raw_id = (task_id or "").strip()
    if not raw_id:
        raise ValueError("task_id is required")
    tag = normalize_client_tag(client_tag)
    profile = get_profile(tag)
    client = DiscoLikeClient()
    payload = client.fetch_task(raw_id)

    class _Replay:
        enabled = True
        calls = 0
        last_error = ""
        on_task_started = None

        def resolve_domains(self, domains, **kwargs):
            companies = kwargs.get("companies") or {}
            parsed = client.parse_results(payload, profile=profile, companies=companies)
            out: dict[str, DiscoDomainResult] = {}
            hosts: list[str] = []
            seen: set[str] = set()
            for domain in domains:
                host = str(domain or "").strip().lower()
                if not host or host in seen:
                    continue
                seen.add(host)
                hosts.append(host)
            for host in hosts:
                row = parsed.get(host) or DiscoDomainResult(domain=host)
                row.cost_usd = 0.0
                out[host] = row
            self.calls += len(hosts)
            return out

    bundle = build_vendors()
    bundle.discolike = _Replay()
    result = resolve_people(
        source_table=source_table,
        where=where,
        client_tag=tag,
        min_tier="discolike",
        max_tier="discolike",
        estimate_only=False,
        write_supabase=True,
        vendors=bundle,
        record_measured_rates=False,
    )
    result["resumed_task_id"] = raw_id
    result["spent_usd"] = 0.0
    result["billing"] = "free_reread"
    counts = result.get("per_tier") or {}
    block = counts.get("discolike") if isinstance(counts, dict) else None
    if isinstance(block, dict):
        block["usd"] = 0.0
    return result
