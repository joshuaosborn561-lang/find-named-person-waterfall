"""Phase-zero receipt: score every tier on ground truth, then write tier_order."""

from __future__ import annotations

from typing import Any, Callable

from . import supabase_sync
from .people import PersonHit, company_matches, looks_like_person
from .progress import build_counter
from .pricing import LiveRates, compute_tier_order, lane_tiers
from .profile import ClientProfile, get_profile, normalize_client_tag, update_profile_metrics
from .source import where_to_filters
from .titles import audit_title
from .waterfall import VendorBundle, _call_tier, build_vendors, read_live_rates

ProgressFn = Callable[[dict[str, Any]], None]


def _page_source(
    schema: str,
    table: str,
    columns: list[str],
    *,
    key_column: str,
    filters: list[dict[str, str]] | None = None,
    pages: int = 12,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = None
    for _ in range(pages):
        data = supabase_sync.rpc(
            "ew_read_source",
            {
                "p_schema": schema,
                "p_table": table,
                "p_filters": filters or [],
                "p_columns": columns,
                "p_key_column": key_column,
                "p_after": cursor,
                "p_limit": 500,
            },
        )
        rows = [r for r in (data or []) if isinstance(r, dict)]
        if not rows:
            break
        out.extend(rows)
        last = rows[-1].get(key_column)
        if last is None:
            break
        cursor = str(last)
        if len(rows) < 500:
            break
    return out


def _gt_contacts(profile: ClientProfile) -> list[dict[str, Any]]:
    ref = profile.contacts_table_ref()
    if not ref.get("table"):
        return []
    return _page_source(
        ref["schema"],
        ref["table"],
        ["domain", "first_name", "last_name", "job_title"],
        key_column="id",
        pages=12,
    )


def _title_matched_contacts(profile: ClientProfile, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        audit = audit_title(
            str(row.get("job_title") or ""),
            target_titles=profile.target_titles,
            title_synonyms=profile.title_synonyms,
            title_exclude_regex=profile.title_exclude_regex,
            seniority_floor=profile.seniority_floor,
        )
        if audit.title_match:
            out.append(row)
    return out


def pick_domain_sample(profile: ClientProfile, n: int) -> list[dict[str, Any]]:
    rows = _title_matched_contacts(profile, _gt_contacts(profile))
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower()
        if not domain:
            continue
        by_domain.setdefault(domain, []).append(row)
    picked: list[dict[str, Any]] = []
    for domain, people in by_domain.items():
        if len(people) < 3:
            continue
        picked.append(
            {
                "domain": domain,
                "company_name": domain,
                "known": {
                    (
                        str(p.get("first_name") or "").strip().lower(),
                        str(p.get("last_name") or "").strip().lower(),
                    )
                    for p in people
                },
                "known_count": len(people),
            }
        )
        if len(picked) >= n:
            break
    return picked


def pick_name_sample(profile: ClientProfile, n: int) -> list[dict[str, Any]]:
    ref = profile.companies_no_domain_ref()
    if not ref.get("table"):
        return []
    filters = []
    where = ref.get("where") or ""
    if where:
        from .source import where_to_filters

        filters = where_to_filters(where)
    key = "place_id"
    data = _page_source(
        ref["schema"],
        ref["table"],
        [
            "company_name",
            "name",
            "contractor_name",
            "clean_name",
            "city",
            "state",
            "address",
            "domain",
            "place_id",
        ],
        key_column=key,
        filters=filters,
        pages=4,
    )
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in data:
        if not isinstance(row, dict):
            continue
        company = str(
            row.get("company_name")
            or row.get("name")
            or row.get("contractor_name")
            or row.get("clean_name")
            or ""
        ).strip()
        if not company or company.lower() in seen:
            continue
        seen.add(company.lower())
        picked.append(
            {
                "domain": "",
                "company_name": company,
                "city": str(row.get("city") or ""),
                "state": str(row.get("state") or ""),
                "known": set(),
                "known_count": 0,
            }
        )
        if len(picked) >= n:
            break
    return picked


def _score_people(
    people: list[PersonHit],
    *,
    profile: ClientProfile,
    company_name: str,
    domain: str,
    known: set[tuple[str, str]],
) -> dict[str, int]:
    returned = 0
    matched = 0
    new = 0
    already = 0
    for person in people:
        if not looks_like_person(person.first_name, person.last_name):
            continue
        if company_name and person.company_name:
            if not company_matches(
                input_company=company_name,
                input_domain=domain,
                returned_company=person.company_name,
                returned_domain=person.domain,
            ):
                continue
        returned += 1
        audit = audit_title(
            person.title,
            target_titles=profile.target_titles,
            title_synonyms=profile.title_synonyms,
            title_exclude_regex=profile.title_exclude_regex,
            seniority_floor=profile.seniority_floor,
        )
        if not audit.title_match:
            continue
        matched += 1
        key = (person.first_name.lower(), person.last_name.lower())
        if key in known:
            already += 1
        else:
            new += 1
    return {
        "people": returned,
        "title_matched": matched,
        "new": new,
        "already_known": already,
    }


def _empty_tier_score() -> dict[str, Any]:
    return {
        "companies": 0,
        "people": 0,
        "title_matched": 0,
        "new": 0,
        "already_known": 0,
        "usd": 0.0,
        "cost_per_title_matched": None,
        "title_match_rate": 0.0,
    }


def run_receipt(
    client_tag: str,
    n: int = 15,
    *,
    approve_cost_usd: float | None = 3.0,
    estimate_only: bool = False,
    write_profile: bool = True,
    progress_callback: ProgressFn | None = None,
    vendors: VendorBundle | None = None,
) -> dict[str, Any]:
    tag = normalize_client_tag(client_tag)
    profile = get_profile(tag)
    bundle = vendors or build_vendors()
    rates = read_live_rates(bundle, probe_search=True)
    order = compute_tier_order(
        rates=rates,
        measured_rates=profile.people_measured_rates,
        dropped_tiers=profile.people_dropped_tiers,
    )

    domain_sample = pick_domain_sample(profile, n)
    name_sample = pick_name_sample(profile, n)

    def estimate() -> dict[str, Any]:
        # Receipt: LeadMagic employee on n domains, role/search on 2n, SERP n, AI Ark 2n.
        lm_unit = rates.unit_usd("leadmagic_employee")
        ark_unit = rates.unit_usd("aiark")
        role_unit = rates.unit_usd("leadmagic_role")
        serp_unit = rates.unit_usd("serp")
        prospeo_unit = rates.unit_usd("prospeo") * 0.5
        est = (
            lm_unit * n * 8
            + ark_unit * (len(domain_sample) + len(name_sample)) * 3
            + serp_unit * len(name_sample)
            + role_unit * (len(domain_sample) + len(name_sample)) * 0.5
            + prospeo_unit * (len(domain_sample) + len(name_sample))
        )
        return {
            "ok": True,
            "estimate_only": True,
            "client_tag": tag,
            "n": n,
            "domain_companies": len(domain_sample),
            "name_companies": len(name_sample),
            "estimated_usd": round(est, 4),
            "live_rates": {
                "leadmagic_per_credit": rates.leadmagic_per_credit,
                "leadmagic_credits": rates.leadmagic_credits,
                "leadmagic_plan": rates.leadmagic_plan,
                "leadmagic_search_free": rates.leadmagic_search_free,
                "aiark_per_credit": rates.aiark_per_credit,
                "notes": rates.notes,
            },
            "tier_order": order,
            "note": "Print this figure before running. Receipt does not reorder tiers.",
        }

    preview = estimate()
    if estimate_only:
        return preview
    if approve_cost_usd is not None and preview["estimated_usd"] > approve_cost_usd:
        return {
            **preview,
            "estimate_only": False,
            "status": "deferred",
            "reason": "estimated_usd exceeds approve_cost_usd",
            "approve_cost_usd": approve_cost_usd,
        }

    scores: dict[str, dict[str, Any]] = {
        "domain": {},
        "name": {},
    }
    spent = 0.0
    receipt_total = len(domain_sample) + len(name_sample)
    receipt_done = 0
    receipt_matched = 0

    def emit_receipt(phase: str, lane: str = "", tier: str = "") -> None:
        if not progress_callback:
            return
        counter = build_counter(
            done=receipt_done,
            total=receipt_total,
            title_matched=receipt_matched,
            phase=phase,
        )
        if lane or tier:
            extra = " · ".join(p for p in (lane, tier) if p)
            counter["message"] = f"{counter['message']} · {extra}"
        progress_callback(
            {
                "status": phase,
                "lane": lane,
                "tier": tier,
                "input_rows": receipt_total,
                "spent_usd": round(spent, 4),
                "counter": counter,
            }
        )

    emit_receipt("running")

    def run_lane(lane: str, sample: list[dict[str, Any]]) -> None:
        nonlocal spent, receipt_done, receipt_matched
        tiers = lane_tiers(order, "domain" if lane == "domain" else "name")
        # Receipt runs every paid tier, including those defaulted off in production.
        extra = []
        if lane == "domain":
            extra = ["prospeo", "leadmagic_role"]
        else:
            extra = ["prospeo", "leadmagic_role", "serp", "aiark"]
        for t in extra:
            if t not in tiers:
                tiers.append(t)
        for tier in tiers:
            scores[lane].setdefault(tier, _empty_tier_score())
        for company in sample:
            for tier in tiers:
                block = scores[lane][tier]
                block["companies"] += 1
                people = _call_tier(
                    tier,
                    bundle,
                    profile=profile,
                    domain=str(company.get("domain") or ""),
                    company_name=str(company.get("company_name") or ""),
                    city=str(company.get("city") or ""),
                    state=str(company.get("state") or ""),
                    titles=list(profile.target_titles),
                )
                tally = _score_people(
                    people,
                    profile=profile,
                    company_name=str(company.get("company_name") or ""),
                    domain=str(company.get("domain") or ""),
                    known=company.get("known") or set(),
                )
                for key in ("people", "title_matched", "new", "already_known"):
                    block[key] += tally[key]
                meta = next((r for r in order if r["tier"] == tier), {})
                unit = float(meta.get("unit_usd") or 0)
                billing = meta.get("billing") or "always"
                cost = 0.0
                if billing == "always":
                    if tier == "serp":
                        cost = unit
                    elif tier == "leadmagic_employee":
                        cost = unit * len(people)
                    else:
                        cost = unit * len(people)
                elif billing == "free_on_miss" and tally["people"]:
                    cost = unit * (1 if tier == "prospeo" else max(tally["people"], 1))
                block["usd"] += cost
                spent += cost
            receipt_done += 1
            receipt_matched = sum(
                int((block or {}).get("title_matched") or 0)
                for lane_scores in scores.values()
                for block in lane_scores.values()
            )
            emit_receipt("running", lane=lane)

    run_lane("domain", domain_sample)
    run_lane("name", name_sample)

    measured: dict[str, Any] = {}
    dropped: list[str] = []
    all_tiers = {row["tier"] for row in order} | set(scores["domain"]) | set(scores["name"])
    for tier in all_tiers:
        d = scores["domain"].get(tier, _empty_tier_score())
        nm = scores["name"].get(tier, _empty_tier_score())
        matched = d["title_matched"] + nm["title_matched"]
        people = d["people"] + nm["people"]
        companies = d["companies"] + nm["companies"]
        usd = d["usd"] + nm["usd"]
        rate = (matched / companies) if companies else 0.0
        for block in (d, nm):
            if block["title_matched"]:
                block["cost_per_title_matched"] = round(
                    block["usd"] / block["title_matched"], 4
                )
            block["title_match_rate"] = (
                round(block["title_matched"] / block["companies"], 4)
                if block["companies"]
                else 0.0
            )
        measured[tier] = {
            "title_matched": matched,
            "people": people,
            "companies": companies,
            "usd": round(usd, 4),
            "title_match_rate": round(rate, 4),
            "cost_per_title_matched": round(usd / matched, 4) if matched else None,
            "domain": d,
            "name": nm,
        }
        if companies and matched == 0 and tier not in {"cache"}:
            dropped.append(tier)

    final_order = compute_tier_order(
        rates=rates,
        measured_rates={k: v["title_match_rate"] for k, v in measured.items()},
        dropped_tiers=dropped,
    )
    if write_profile:
        try:
            update_profile_metrics(
                tag,
                tier_order=final_order,
                dropped_tiers=dropped,
                measured_rates=measured,
            )
        except Exception as exc:  # noqa: BLE001
            rates.notes.append(f"profile write failed: {type(exc).__name__}")

    result = {
        "ok": True,
        "client_tag": tag,
        "n": n,
        "estimated_usd": preview["estimated_usd"],
        "spent_usd": round(spent, 4),
        "domain_companies": len(domain_sample),
        "name_companies": len(name_sample),
        "input_rows": receipt_total,
        "counter": build_counter(
            done=receipt_done,
            total=receipt_total,
            title_matched=receipt_matched,
            phase="completed",
        ),
        "per_tier": measured,
        "people_dropped_tiers": dropped,
        "people_tier_order": final_order,
        "dropped_tiers": dropped,
        "tier_order": final_order,
        "live_rates": preview["live_rates"],
        "status": "completed",
    }
    emit_receipt("completed")
    return result
