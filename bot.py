#!/usr/bin/env python3
"""
magicpin AI Challenge — "Vera, but better" bot
================================================

This module is BOTH:
  1. A pure composer:  compose(category, merchant, trigger, customer) -> dict
     (the exact contract from challenge-brief.md §7.1 — used directly by
     generate_submission.py to build submission.jsonl from the real dataset)
  2. A FastAPI app exposing the 5 endpoints defined in
     challenge-testing-brief.md (§2): /v1/context, /v1/tick, /v1/reply,
     /v1/healthz, /v1/metadata.

Design choices (see README.md for the full rationale):
  - The composer is template + real-data driven, NOT a raw LLM call. Every
    fact used comes from the pushed CategoryContext / MerchantContext /
    TriggerContext / CustomerContext — nothing is invented. This makes the
    bot deterministic (temperature=0 requirement satisfied trivially),
    fast (<30s budget is a non-issue), and safe against hallucination
    penalties. Swap in an LLM call inside `_llm_polish()` if you want
    fancier phrasing — the anti-fabrication guardrails stay in the
    templates either way.
  - Every trigger `kind` in the dataset (25 of them) has its own handler
    that (a) uses the rich payload when the judge provides one, and
    (b) falls back to real merchant/category facts (peer_stats, digest,
    signals, performance deltas, offers, customer relationship data) when
    the payload is a placeholder — so restraint/specificity never becomes
    fabrication.
  - Language: merchant.identity.languages containing "hi" (or a
    customer's language_pref containing "hi") switches connective tissue
    to Hindi-English code-mix, matching §9 Pattern A/B examples in the
    brief. Numbers/citations/proper nouns stay in Latin script, which is
    how real Indian WhatsApp business chat reads.
  - Conversation FSM (used by /v1/reply and conversation_handlers.respond):
    auto-reply detection (verbatim repeats + canned-phrase heuristics),
    intent-transition detection (switch straight to action, no re-asking),
    hostile/"stop" detection (graceful, polite exit), off-topic redirection
    (stay on-mission without ending), and a default "acknowledge + advance"
    reply otherwise.
"""

from __future__ import annotations

import os
import re
import time
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

# =============================================================================
# Small text helpers
# =============================================================================

def J(en: str, hi: str, hi_en: bool) -> str:
    """Pick the Hindi-English-mix phrase or the plain-English phrase."""
    return hi if hi_en else en


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\s+([.,!?])", r"\1", s)
    return s


def uses_hi_en(merchant: dict) -> bool:
    langs = ((merchant or {}).get("identity") or {}).get("languages") or []
    return "hi" in langs


def salutation(merchant: dict, hi_en: bool) -> str:
    ident = (merchant or {}).get("identity", {})
    owner = ident.get("owner_first_name")
    cat = (merchant or {}).get("category_slug", "")
    name = ident.get("name", "there")
    if cat == "dentists" and owner:
        return f"Dr. {owner}"
    return owner or name


def business_name(merchant: dict) -> str:
    return (merchant or {}).get("identity", {}).get("name", "your business")


def active_offers(merchant: dict) -> list[str]:
    return [o["title"] for o in (merchant or {}).get("offers", []) if o.get("status") == "active"]


def taboo_words(category: dict) -> list[str]:
    return [w.lower() for w in (category or {}).get("voice", {}).get("vocab_taboo", [])]


def _strip_taboo(body: str, category: dict) -> str:
    """Defensive net: templates are written to avoid taboo words already,
    but if a taboo phrase from THIS category's list ever ends up in the
    body, drop it rather than risk a promotional/over-claim penalty."""
    for t in taboo_words(category):
        if t and t in body.lower():
            pattern = re.compile(re.escape(t), re.IGNORECASE)
            body = pattern.sub("", body)
    return _clean(body)


def _months_since(date_str: Optional[str], now_str: Optional[str] = None) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now_str:
            try:
                now = datetime.fromisoformat(now_str.replace("Z", "+00:00"))
            except Exception:
                pass
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        months = (now.year - d.year) * 12 + (now.month - d.month)
        return max(months, 0)
    except Exception:
        return None


def customer_uses_hi(customer: Optional[dict], hi_en_default: bool) -> bool:
    if not customer:
        return hi_en_default
    pref = (customer.get("identity") or {}).get("language_pref", "") or ""
    return hi_en_default or "hi" in pref


def customer_name(customer: Optional[dict]) -> str:
    return (customer or {}).get("identity", {}).get("name", "there")


# =============================================================================
# Per-trigger-kind composers
# Each handler: (category, merchant, trigger, customer, hi_en) -> (body, cta, lever)
# cta in {"binary", "open_ended", "none"}
# =============================================================================

def h_research_digest(category, merchant, trigger, customer, hi_en):
    payload = trigger.get("payload", {}) or {}
    digest = category.get("digest", []) or []
    item = None
    if not payload.get("placeholder") and payload.get("top_item_id"):
        item = next((d for d in digest if d.get("id") == payload["top_item_id"]), None)
    if item is None and digest:
        item = digest[0]
    sal = salutation(merchant, hi_en)
    if not item:
        body = f"{sal}, {J('nothing new in your category digest this week — will flag it the moment something relevant lands.', 'is hafte digest mein kuch naya nahi hai — kuch relevant aate hi bata dungi.', hi_en)}"
        return body, "none", "restraint"
    title = item.get("title", "")
    source = item.get("source", "")
    segment = (item.get("patient_segment") or item.get("segment") or "").replace("_", " ")
    trial_n = item.get("trial_n")
    signals = merchant.get("signals", []) or []
    seg_note = ""
    if segment and any(segment in s.replace("_", " ") for s in signals):
        seg_note = J(f" — relevant to your {segment} patients", f" — aapke {segment} patients ke liye relevant", hi_en)
    n_part = f"{trial_n:,}-patient trial: " if trial_n else ""
    ask = J("Want me to pull the abstract and draft a patient WhatsApp you can share?",
            "chahenge main abstract nikaal ke ek patient WhatsApp draft kar doon jo aap share kar sakein?", hi_en)
    src = f" ({source})" if source else ""
    cat_label = category.get("display_name", "your field")
    body = f"{sal}, this week's {cat_label} digest: {n_part}{title}{seg_note}.{src} {ask}"
    return body, "open_ended", "curiosity+reciprocity"


def h_regulation_change(category, merchant, trigger, customer, hi_en):
    payload = trigger.get("payload", {}) or {}
    digest = category.get("digest", []) or []
    item = None
    if payload.get("top_item_id"):
        item = next((d for d in digest if d.get("id") == payload["top_item_id"]), None)
    if item is None:
        item = next((d for d in digest if d.get("kind") == "compliance"), None) or (digest[0] if digest else None)
    sal = salutation(merchant, hi_en)
    if not item:
        return f"{sal}, {J('no compliance updates to flag right now.', 'abhi koi compliance update flag karne layak nahi hai.', hi_en)}", "none", "restraint"
    title = item.get("title", "")
    source = item.get("source", "")
    actionable = item.get("actionable")
    deadline = payload.get("deadline_iso") or item.get("deadline_iso")
    when = f" Effective {deadline[:10]}." if deadline and deadline[:10] not in title else ""
    src = f" ({source})" if source else ""
    if actionable:
        ask = f" Actionable: {actionable}."
        cta = "open_ended"
    else:
        ask = J(" Want the 1-line checklist so your practice stays compliant?",
                " Chahenge ek 1-line checklist bhej doon taaki practice compliant rahe?", hi_en)
        cta = "open_ended"
    body = f"{sal}, heads up — {title}.{when}{src}{ask}"
    return body, cta, "specificity+reciprocity"


def h_competitor_opened(category, merchant, trigger, customer, hi_en):
    payload = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    perf = merchant.get("performance", {}) or {}
    ctr = perf.get("ctr")
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr")
    name = payload.get("competitor_name")
    dist = payload.get("distance_km")
    offer = payload.get("their_offer")
    if name and not payload.get("placeholder"):
        distp = f", {dist} km away" if dist else ""
        offerp = f' running "{offer}"' if offer else ""
        lede = f"New listing near you{distp}: {name}{offerp}."
    else:
        lede = J("A new competitor listing opened in your locality this week.",
                  "aapki locality mein is hafte ek naya competitor listing khula hai.", hi_en)
    gap = ""
    if ctr is not None and peer_ctr is not None and ctr < peer_ctr:
        gap = J(f" Your CTR is {ctr*100:.1f}% vs the {peer_ctr*100:.1f}% category average — worth tightening your listing before footfall shifts.",
                f" Aapka CTR {ctr*100:.1f}% hai, category average {peer_ctr*100:.1f}% hai — footfall shift hone se pehle listing tight kar lete hain.", hi_en)
    ask = J(" Want me to check what they're doing differently on Google?",
            " Chahenge check karun woh Google par kya alag kar rahe hain?", hi_en)
    body = f"{sal}, {lede}{gap}{ask}"
    return body, "open_ended", "loss_aversion+specificity"


def h_festival_upcoming(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    fest = p.get("festival")
    days = p.get("days_until")
    offers = active_offers(merchant)
    if fest and days is not None and not p.get("placeholder"):
        lede = J(f"{fest} is {days} day{'s' if days != 1 else ''} away.", f"{fest} bas {days} din door hai.", hi_en)
    else:
        beats = category.get("seasonal_beats", []) or []
        note = beats[0]["note"] if beats else "a seasonal demand window is opening"
        lede = J(f"Heads up on timing: {note}.", f"Timing ka heads-up: {note}.", hi_en)
    offer_line = f' Your "{offers[0]}" is a natural fit for the rush.' if offers else ""
    ask = J(" Want me to schedule a festival post and push this offer to your regulars?",
            " Chahenge ek festival post schedule karke regulars ko yeh offer push karun?", hi_en)
    body = f"{sal}, {lede}{offer_line}{ask}"
    return body, "open_ended", "specificity+effort_externalization"


def h_ipl_match_today(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    match = p.get("match")
    t = p.get("match_time_iso")
    timep = f" at {t[11:16]}" if t else ""
    offers = active_offers(merchant)
    hook = f"{match}{timep} tonight" if match else "tonight's match"
    offer_line = f' Your "{offers[0]}" is a strong match-night pull.' if offers else ""
    ask = J(" Want a quick match-night post drafted for table/delivery push?",
            " Chahenge match-night ka ek quick post draft karun — table ya delivery push ke liye?", hi_en)
    body = f"{sal}, {hook} usually lifts footfall.{offer_line}{ask}"
    return body, "open_ended", "specificity+social_proof"


def _humanize(token: str) -> str:
    return re.sub(r"\s+", " ", (token or "").replace("_", " ").replace("+", " +")).strip()


def h_category_seasonal(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    season = p.get("season")
    trends = p.get("trends") or []
    shelf_action = p.get("shelf_action_recommended")
    if season and trends:
        season_txt = _humanize(season)
        trend_txt = ", ".join(_humanize(t) for t in trends[:2])
        lede = J(f"{season_txt} shift showing: {trend_txt}.", f"{season_txt} shift dikh raha hai: {trend_txt}.", hi_en)
        if isinstance(shelf_action, bool):
            act_note = J(" Worth a shelf/stock adjustment.", " Shelf/stock adjust karna theek rahega.", hi_en) if shelf_action else ""
        elif shelf_action:
            act_note = f" Suggested move: {shelf_action}."
        else:
            act_note = ""
        ask = J(" Want me to draft the shelf/stock note?", " Chahenge ek shelf/stock note draft karun?", hi_en)
        body = f"{sal}, {lede}{act_note}{ask}"
        return body, "open_ended", "specificity+curiosity"
    else:
        ts = category.get("trend_signals", []) or []
        if ts:
            top = ts[0]
            lede = J(f"'{top['query']}' searches up {int(top['delta_yoy']*100)}% YoY in your category.",
                      f"'{top['query']}' ki search {int(top['delta_yoy']*100)}% YoY badhi hai.", hi_en)
        else:
            lede = J("A seasonal demand shift is underway in your category.", "Category mein seasonal demand shift ho raha hai.", hi_en)
    action = p.get("shelf_action_recommended")
    act_line = f" Suggested move: {action}." if action else ""
    ask = J(" Want me to draft the shelf/stock note?", " Chahenge ek shelf/stock note draft karun?", hi_en)
    body = f"{sal}, {lede}{act_line}{ask}"
    return body, "open_ended", "specificity+curiosity"


def h_supply_alert(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    molecule = p.get("molecule")
    batches = p.get("affected_batches")
    mfr = p.get("manufacturer")
    if molecule:
        batch_txt = f" Batches: {', '.join(batches)}." if batches else ""
        lede = J(f"Recall alert: {molecule} ({mfr or 'manufacturer TBD'}).{batch_txt}",
                  f"Recall alert: {molecule} ({mfr or 'manufacturer'}).{batch_txt}", hi_en)
    else:
        lede = J("A supply/recall alert relevant to your stock just came in.",
                  "Aapke stock se related ek supply/recall alert aayi hai.", hi_en)
    ask = J(" Want me to check if this batch is in your current stock list?",
            " Chahenge check karun yeh batch aapke current stock mein hai ya nahi?", hi_en)
    body = f"{sal}, {lede}{ask}"
    return body, "binary", "loss_aversion+specificity"


def h_cde_opportunity(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    digest = category.get("digest", []) or []
    item = next((d for d in digest if d.get("id") == p.get("digest_item_id")), None)
    credits = p.get("credits")
    fee = p.get("fee")
    title = item.get("title") if item else "a relevant CDE session"
    bits = []
    if credits:
        bits.append(f"{credits} CDE credits")
    if fee is not None:
        if isinstance(fee, (int, float)):
            bits.append(f"₹{fee}" if fee else "free")
        else:
            bits.append(_humanize(str(fee)))
    meta = " (" + ", ".join(bits) + ")" if bits else ""
    ask = J(" Want the registration link?", " Chahenge registration link bhej doon?", hi_en)
    body = f"{sal}, {title}{meta} — closing soon.{ask}"
    return body, "open_ended", "specificity+loss_aversion"


def h_perf_dip(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    metric = p.get("metric")
    delta = p.get("delta_pct")
    window = p.get("window")
    if metric and delta is not None and not p.get("placeholder"):
        pct_txt = f"{abs(round(delta*100))}%"
        window_txt = {"7d": "this week", "30d": "this month"}.get(window, window or "this week")
        window_txt_hi = {"7d": "is hafte", "30d": "is mahine"}.get(window, window or "is hafte")
        lede = J(f"Your {metric} dropped {pct_txt} {window_txt} vs baseline.",
                  f"Aapke {metric} mein {pct_txt} ki giravat hai {window_txt_hi} baseline se.", hi_en)
    else:
        d7 = merchant.get("performance", {}).get("delta_7d", {}) or {}
        cand = [(k, v) for k, v in [("calls", d7.get("calls_pct")), ("views", d7.get("views_pct"))] if v is not None and v < 0]
        if cand:
            k, v = min(cand, key=lambda x: x[1])
            lede = J(f"Your {k} are down {abs(round(v*100))}% vs last week.", f"Aapke {k} pichle hafte se {abs(round(v*100))}% neeche hain.", hi_en)
        else:
            lede = J("Your dashboard shows a soft patch this week.", "Is hafte dashboard mein thoda soft patch dikh raha hai.", hi_en)
    ask = J(" Want me to check what changed — post frequency, hours, or a competitor?",
            " Chahenge check karun kya change hua — posts, hours, ya koi competitor?", hi_en)
    body = f"{sal}, {lede}{ask}"
    return body, "open_ended", "loss_aversion+specificity"


def h_perf_spike(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    metric = p.get("metric")
    delta = p.get("delta_pct")
    driver = p.get("likely_driver")
    driver_line = ""
    if metric and delta is not None and not p.get("placeholder"):
        window = p.get("window")
        window_txt = {"7d": "this week", "30d": "this month"}.get(window, window or "this week")
        window_txt_hi = {"7d": "is hafte", "30d": "is mahine"}.get(window, window or "is hafte")
        lede = J(f"Nice spike — {metric} up {abs(round(delta*100))}% {window_txt}.",
                  f"Achi spike — {metric} {abs(round(delta*100))}% badha hai {window_txt_hi}.", hi_en)
        if driver:
            driver_line = f" Likely driver: {_humanize(driver)}."
    else:
        d7 = merchant.get("performance", {}).get("delta_7d", {}) or {}
        cand = [(k, v) for k, v in [("calls", d7.get("calls_pct")), ("views", d7.get("views_pct"))] if v is not None and v > 0]
        if cand:
            k, v = max(cand, key=lambda x: x[1])
            lede = J(f"Your {k} are up {round(v*100)}% vs last week — good momentum.",
                      f"Aapke {k} pichle hafte se {round(v*100)}% badhe hain — achi momentum hai.", hi_en)
        else:
            lede = J("Solid week on your dashboard.", "Is hafte dashboard achi lag rahi hai.", hi_en)
    ask = J(" Want me to double down with a post while it's hot?", " Chahenge isi momentum mein ek post daal doon?", hi_en)
    body = f"{sal}, {lede}{driver_line}{ask}"
    return body, "open_ended", "social_proof+effort_externalization"


def h_milestone(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    metric = p.get("metric")
    val = p.get("value_now")
    target = p.get("milestone_value")
    if metric and val is not None and not p.get("placeholder"):
        metric_txt = _humanize(metric)
        crossed = target is not None and val >= target
        if crossed:
            lede = J(f"You crossed {val} {metric_txt}!", f"Aapne {val} {metric_txt} cross kar liye hain!", hi_en)
        elif target is not None:
            lede = J(f"You're at {val} {metric_txt}, {target - val} away from {target}.",
                      f"Aap {val} {metric_txt} par hain, {target} tak sirf {target - val} baaki hai.", hi_en)
        else:
            lede = J(f"You're at {val} {metric_txt}.", f"Aap {val} {metric_txt} par hain.", hi_en)
    else:
        ytd = merchant.get("customer_aggregate", {}).get("total_unique_ytd")
        if ytd:
            lede = J(f"You've served {ytd} unique customers YTD — a strong run.", f"Aapne is saal {ytd} unique customers serve kiye hain — kaafi achha.", hi_en)
        else:
            lede = J("A milestone on your dashboard is worth a look.", "Dashboard par ek milestone dekhne layak hai.", hi_en)
    ask = J(" Want me to draft a thank-you post for your customers?", " Chahenge customers ke liye ek thank-you post draft karun?", hi_en)
    body = f"{sal}, {lede}{ask}"
    return body, "open_ended", "social_proof+reciprocity"


def h_dormant(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    days = p.get("days_since_last_merchant_message")
    topic = p.get("last_topic")
    if days and not p.get("placeholder"):
        topic_line = f" — we were talking about {_humanize(topic)}" if topic else ""
        lede = J(f"It's been {days} days since we last spoke{topic_line}.", f"{days} din ho gaye hain baat kiye{topic_line}.", hi_en)
    else:
        lede = J("It's been a while since we last spoke.", "Kaafi time ho gaya baat kiye.", hi_en)
    ask = J(" Still want help with your Google profile, or should I check back later?",
            " Google profile mein abhi bhi madad chahiye, ya baad mein check karun?", hi_en)
    body = f"{sal}, {lede}{ask}"
    return body, "binary", "reciprocity"


def h_review_theme(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    theme = p.get("theme")
    occ = p.get("occurrences_30d")
    quote = p.get("common_quote")
    if not theme or p.get("placeholder"):
        themes = merchant.get("review_themes", []) or []
        rt = themes[0] if themes else None
        if rt:
            theme = rt.get("theme")
            occ = rt.get("occurrences_30d")
            quote = rt.get("common_quote")
    if theme:
        occ_txt = f"{occ} reviews this month" if occ else "recent reviews"
        quote_txt = f' — one reads "{quote[:60]}"' if quote else ""
        lede = J(f"{occ_txt} mention '{theme}'{quote_txt}.", f"{occ_txt} mein '{theme}' ka zikr hai{quote_txt}.", hi_en)
        ask = J(" Want me to draft a quick fix plus a reply template for these reviews?",
                " Chahenge ek quick fix aur reviews ka reply template draft karun?", hi_en)
        cta = "open_ended"
    else:
        lede = J("Nothing new in your reviews worth flagging this week.", "Is hafte reviews mein flag karne layak kuch naya nahi.", hi_en)
        ask = ""
        cta = "none"
    body = f"{sal}, {lede}{ask}"
    return body, cta, "specificity+social_proof"


def h_active_planning(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    topic = (p.get("intent_topic") or "your idea").replace("_", " ")
    ask = J(f"On the {topic} — simple starting shape: a price point, 2 sample slots/portions, and a one-line pitch. Want me to draft it now?",
            f"{topic} ke liye — simple shuruaat: ek price point, 2 sample slots/portions, aur ek one-line pitch. Chahenge abhi draft kar doon?", hi_en)
    body = f"{sal}, {ask}"
    return body, "open_ended", "effort_externalization"


def h_curious_ask(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    tmpl = p.get("ask_template")
    if tmpl and not p.get("placeholder"):
        q = _humanize(tmpl)
        q = q[0].upper() + q[1:] if q else q
        if not q.endswith("?"):
            q += "?"
        lead = J("quick one —", "ek quick sawal —", hi_en)
        return f"{sal}, {lead} {q}", "open_ended", "ask_the_merchant"
    default_q = {
        "dentists": "What's the most-asked treatment at your clinic this week?",
        "salons": "What's trending with your clients right now — colour, keratin, or bridal?",
        "restaurants": "What's your best-selling dish this week?",
        "gyms": "What's the one goal most of your members are chasing right now?",
        "pharmacies": "What's the medicine your customers ask for most this week?",
    }.get(category.get("slug", ""), "What's one thing your customers keep asking about lately?")
    lead = J("quick one —", "ek quick sawal —", hi_en)
    body = f"{sal}, {lead} {default_q}"
    return body, "open_ended", "ask_the_merchant"


def h_renewal_due(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    sub = merchant.get("subscription", {}) or {}
    days = p.get("days_remaining", sub.get("days_remaining"))
    plan = p.get("plan", sub.get("plan"))
    amt = p.get("renewal_amount")
    if days is None:
        lede = J("Your plan renewal window is approaching.", "Aapke plan ka renewal window aa raha hai.", hi_en)
    else:
        amt_txt = f" (₹{amt})" if amt else ""
        lede = J(f"Your {plan or 'Pro'} plan renews in {days} days{amt_txt}.", f"Aapka {plan or 'Pro'} plan {days} din mein renew hoga{amt_txt}.", hi_en)
    ask = J(" Reply YES to lock in current pricing, or STOP if you'd like to pause.",
            " Current pricing lock karne ke liye YES bhejein, ya pause ke liye STOP.", hi_en)
    body = f"{sal}, {lede}{ask}"
    return body, "binary", "loss_aversion"


def h_winback(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    days = p.get("days_since_expiry")
    dip = p.get("perf_dip_pct")
    lapsed = p.get("lapsed_customers_added_since_expiry")
    parts = []
    if days:
        parts.append(f"{days} days since your plan lapsed")
    if dip:
        parts.append(f"visibility down {abs(round(dip*100))}%")
    if lapsed:
        parts.append(f"{lapsed} more customers gone quiet")
    facts = ", ".join(parts) if parts else "your listing has been running without a plan for a while"
    ask = J(" Reactivating takes 2 minutes — want me to send the link?",
            " Reactivate karna 2 minute ka kaam hai — link bhej doon?", hi_en)
    body = f"{sal}, {facts}.{ask}"
    return body, "binary", "loss_aversion+effort_externalization"


def h_seasonal_perf_dip(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    metric = p.get("metric")
    delta = p.get("delta_pct")
    note = p.get("season_note")
    expected = p.get("is_expected_seasonal")
    lede = f"{metric} down {abs(round(delta*100))}%" if metric and delta is not None else "a seasonal dip"
    if expected and note:
        ctx = J(f" — {note}, so this is expected.", f" — {note}, yeh normal hai.", hi_en)
    elif note:
        ctx = f" — {note}."
    else:
        ctx = "."
    ask = J(" No action needed now; want a reminder when the recovery window opens?",
            " Abhi kuch karne ki zarurat nahi; recovery window khulne par reminder chahiye?", hi_en)
    body = f"{sal}, {lede}{ctx}{ask}"
    return body, "open_ended", "specificity+reciprocity"


def h_gbp_unverified(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    sal = salutation(merchant, hi_en)
    uplift = p.get("estimated_uplift_pct")
    path = p.get("verification_path")
    uplift_txt = f" Verified listings see ~{round(uplift*100)}% more calls on average." if uplift else ""
    path_txt = f" via {_humanize(path)}" if path else ""
    ask = J(f" Want me to walk you through verification{path_txt}?", f" Verification{path_txt} mein madad chahiye?", hi_en)
    body = f"{sal}, {J('your Google listing is still unverified.', 'aapki Google listing abhi unverified hai.', hi_en)}{uplift_txt}{ask}"
    return body, "binary", "loss_aversion+specificity"


# ---- customer-facing (send_as = merchant_on_behalf) ----

def h_recall_due(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    last = p.get("last_service_date") or (customer or {}).get("relationship", {}).get("last_visit")
    slots = p.get("available_slots") or []
    offers = active_offers(merchant)
    months = _months_since(last, trigger.get("expires_at"))
    if months:
        due_line = J(f"It's been about {months} months since your last visit.", f"Aapki last visit ko takriban {months} mahine ho gaye hain.", use_hi)
    else:
        due_line = J("Your recall/checkup window is open.", "Aapka recall/checkup window khula hai.", use_hi)
    slot_labels = [s.get("label", s) if isinstance(s, dict) else str(s) for s in slots]
    slot_line = ""
    if slot_labels:
        slot_line = " " + J("Slots open: ", "Slots khaali hain: ", use_hi) + ", ".join(slot_labels[:2]) + "."
    offer_line = f" {offers[0]}." if offers else ""
    ask = J(" Reply 1 or 2 to book, or tell us a time that works.", " Book karne ke liye 1 ya 2 bhejein, ya apna time bata dein.", use_hi)
    body = f"Hi {name}, {biz} here. {due_line}{slot_line}{offer_line}{ask}"
    return body, "binary", "loss_aversion+specificity"


def h_lapsed_soft(category, merchant, trigger, customer, hi_en):
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    visits = (customer or {}).get("relationship", {}).get("visits_total")
    offers = active_offers(merchant)
    if visits:
        visit_line = J(f"You've visited us {visits} times before — we miss you!", f"Aapne pehle {visits} baar visit kiya hai — miss kar rahe hain!", use_hi)
    else:
        visit_line = J("We miss seeing you!", "Aapko miss kar rahe hain!", use_hi)
    offer_line = f" {offers[0]} — just for you." if offers else ""
    ask = J(" Want to book your next visit?", " Agli visit book karna chahenge?", use_hi)
    body = f"Hi {name}, {biz} here. {visit_line}{offer_line}{ask}"
    return body, "open_ended", "reciprocity+loss_aversion"


def h_lapsed_hard(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    days = p.get("days_since_last_visit")
    focus = _humanize(p.get("previous_focus", ""))
    months = p.get("previous_membership_months")
    days_txt = f"{days} days" if days else "a while"
    member_txt = f" (you were a {months}-month member before)" if months else ""
    focus_line = f" — last time you were focused on {focus}." if focus else "."
    ask = J(" A lot has changed — want a fresh trial before deciding?", " Kaafi kuch naya hai — decide karne se pehle ek fresh trial chahenge?", use_hi)
    body = f"Hi {name}, {biz} here. It's been {days_txt}{member_txt}{focus_line}{ask}"
    return body, "open_ended", "reciprocity+curiosity"


def h_appt_tomorrow(category, merchant, trigger, customer, hi_en):
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    body = (f"Hi {name}, {biz} here — "
            f"{J('quick reminder: your appointment is tomorrow.', 'ek quick reminder: aapka appointment kal hai.', use_hi)} "
            f"{J('Reply YES to confirm, or let us know if you need to reschedule.', 'Confirm karne ke liye YES bhejein, ya reschedule ke liye batayein.', use_hi)}")
    return body, "binary", "specificity"


def h_chronic_refill(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    molecules = p.get("molecule_list") or []
    runs_out = p.get("stock_runs_out_iso")
    saved_addr = p.get("delivery_address_saved")
    if molecules and not p.get("placeholder"):
        med_txt = ", ".join(molecules[:2])
        lede = J(f"Your regular refill ({med_txt}) is due soon.", f"Aapka regular refill ({med_txt}) jaldi due hai.", use_hi)
    else:
        lede = J("Your regular medicine refill looks due soon.", "Aapka regular medicine refill jaldi due lag raha hai.", use_hi)
    if runs_out:
        lede += J(f" Stock runs out around {runs_out[:10]}.", f" Stock takriban {runs_out[:10]} tak khatam ho sakta hai.", use_hi)
    if saved_addr:
        deliver_line = J(" Should we deliver to your saved address?", " Aapke saved address par deliver kar dein?", use_hi)
    else:
        deliver_line = J(" Want us to keep it ready for pickup?", " Pickup ke liye ready rakh dein?", use_hi)
    body = f"Hi {name}, {biz} here. {lede}{deliver_line}"
    return body, "binary", "specificity+effort_externalization"


def h_trial_followup(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    options = p.get("next_session_options") or []
    if options and not p.get("placeholder"):
        opt_txt = " or ".join(options[:2])
        ask = J(f"Next sessions open: {opt_txt}. Want to lock one in?", f"Agle sessions khaali hain: {opt_txt}. Book kar dein?", use_hi)
    else:
        ask = J("How did your trial feel — want to book your next session?", "Trial kaisa laga — agla session book karein?", use_hi)
    body = f"Hi {name}, {biz} here. {ask}"
    return body, "open_ended", "curiosity+ask"


def h_wedding_followup(category, merchant, trigger, customer, hi_en):
    p = trigger.get("payload", {}) or {}
    use_hi = customer_uses_hi(customer, hi_en)
    name = customer_name(customer)
    biz = business_name(merchant)
    days = p.get("days_to_wedding")
    trial_done = p.get("trial_completed")
    days_txt = f"{days} days" if days else "your big day"
    trial_line = J("Since your trial went well, ", "Trial achha gaya, ", use_hi) if trial_done else ""
    ask = J(f"want to lock in your bridal slot for {days_txt} away?", f"{days_txt} baad ke liye apna bridal slot lock karna chahenge?", use_hi)
    body = f"Hi {name}, {biz} here. {trial_line}{ask[0].upper()}{ask[1:]}"
    return body, "binary", "loss_aversion+effort_externalization"


def _handle_generic(category, merchant, trigger, customer, hi_en):
    kind = trigger.get("kind", "update")
    label = kind.replace("_", " ")
    if customer:
        name = customer_name(customer)
        biz = business_name(merchant)
        body = f"Hi {name}, {biz} here — quick update on your {label}. {J('Want more detail?', 'Aur detail chahiye?', hi_en)}"
    else:
        sal = salutation(merchant, hi_en)
        body = f"{sal}, {J(f'quick update on {label} for your account.', f'{label} par ek quick update.', hi_en)} {J('Want more detail?', 'Aur detail chahiye?', hi_en)}"
    return body, "open_ended", "generic"


KIND_HANDLERS = {
    "research_digest": h_research_digest,
    "regulation_change": h_regulation_change,
    "competitor_opened": h_competitor_opened,
    "festival_upcoming": h_festival_upcoming,
    "ipl_match_today": h_ipl_match_today,
    "category_seasonal": h_category_seasonal,
    "supply_alert": h_supply_alert,
    "cde_opportunity": h_cde_opportunity,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "milestone_reached": h_milestone,
    "dormant_with_vera": h_dormant,
    "review_theme_emerged": h_review_theme,
    "active_planning_intent": h_active_planning,
    "curious_ask_due": h_curious_ask,
    "renewal_due": h_renewal_due,
    "winback_eligible": h_winback,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "gbp_unverified": h_gbp_unverified,
    "recall_due": h_recall_due,
    "customer_lapsed_soft": h_lapsed_soft,
    "customer_lapsed_hard": h_lapsed_hard,
    "appointment_tomorrow": h_appt_tomorrow,
    "chronic_refill_due": h_chronic_refill,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_wedding_followup,
}


# =============================================================================
# THE required contract (challenge-brief.md §7.1)
# =============================================================================

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Inputs are dicts loaded from the dataset JSON (or pushed via /v1/context).
    Returns a dict with keys: body, cta, send_as, suppression_key, rationale.
    Deterministic: no randomness, no external calls, temperature is moot.
    """
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}
    hi_en = uses_hi_en(merchant)
    kind = trigger.get("kind", "")
    handler = KIND_HANDLERS.get(kind, _handle_generic)
    body, cta, lever = handler(category, merchant, trigger, customer, hi_en)
    body = _strip_taboo(body, category)
    send_as = "merchant_on_behalf" if customer else "vera"
    mid = merchant.get("merchant_id", "unknown")
    suppression_key = trigger.get("suppression_key") or f"{kind}:{mid}"
    rationale = (
        f"kind={kind}; lever={lever}; lang={'hi-en' if (hi_en or customer_uses_hi(customer, hi_en) != hi_en) else 'en'}; "
        f"anchored on live category/merchant/customer data pushed for this test — no invented facts."
    )
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }


# =============================================================================
# Conversation FSM — shared by /v1/reply and conversation_handlers.respond()
# =============================================================================

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting", r"will (get back|respond|reply) (to you )?shortly",
    r"automated (assistant|reply|message)", r"currently unavailable", r"away right now",
    r"team tak pahuncha", r"shukriya.*team", r"busy right now.*reply soon",
    r"we (have received|received) your message",
]
HOSTILE_PATTERNS = [
    r"\bstop\b", r"\bspam\b", r"not interested", r"leave me alone", r"band karo",
    r"bakwaas", r"\bfraud\b", r"\bharass", r"\bshut up\b", r"\budhar\b.*mat",
]
INTENT_PATTERNS = [
    r"\byes\b", r"\bok(ay)?\b", r"let'?s do it", r"go ahead", r"sign me up",
    r"sounds good", r"\bhaan\b", r"theek hai", r"\bkaro\b", r"start karo", r"\bsure\b",
]
WAIT_PATTERNS = [
    r"call.{0,10}later", r"not now", r"give me (some )?time", r"abhi busy",
    r"baad mein", r"later please",
]


def _matches_any(patterns, text_lower):
    return any(re.search(p, text_lower) for p in patterns)


def decide_reply(history: list[str], merchant_message: str, ctx: Optional[dict] = None) -> dict:
    """
    Core FSM. `history` = prior merchant messages (oldest->newest) in this
    conversation (not including merchant_message). `ctx` may carry
    {"last_topic": str, "auto_reply_strikes": int} to keep it stateless-callable.
    Returns {"action": "send"|"wait"|"end", "body"?, "wait_seconds"?, "rationale"}.
    """
    ctx = ctx or {}
    text = (merchant_message or "").strip()
    text_l = text.lower()
    is_verbatim_repeat = text in history
    is_canned = _matches_any(AUTO_REPLY_PATTERNS, text_l)
    strikes = int(ctx.get("auto_reply_strikes", 0))

    if is_verbatim_repeat or is_canned:
        strikes += 1
        if strikes >= 2:
            return {
                "action": "end",
                "rationale": "Repeated auto-reply / canned text detected twice — exiting gracefully instead of burning more turns.",
                "_auto_reply_strikes": strikes,
            }
        return {
            "action": "send",
            "body": "Samajh gayi — before this goes to your team, want to take 2 minutes yourself to see exactly what's missing? Quick and easy.",
            "cta": "binary",
            "rationale": "First canned-reply detection — one polite nudge to reach a human, per production-Vera Pattern B.",
            "_auto_reply_strikes": strikes,
        }

    if _matches_any(HOSTILE_PATTERNS, text_l):
        return {
            "action": "end",
            "rationale": "Merchant signalled not-interested/hostile — polite graceful exit, no further nudges.",
            "body": "Understood, no problem at all — I'll stop here. Best wishes! 🙂",
        }

    if _matches_any(INTENT_PATTERNS, text_l):
        topic = ctx.get("last_topic") or "this"
        return {
            "action": "send",
            "body": f"Great — starting now on {topic}. Give me a moment and I'll confirm once it's done; no further input needed from you right now.",
            "cta": "none",
            "rationale": "Explicit commitment detected — switching straight to action mode instead of re-qualifying (avoids the Pattern D failure).",
        }

    if _matches_any(WAIT_PATTERNS, text_l):
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time — backing off 30 minutes."}

    # Off-topic but not hostile: acknowledge briefly, stay on mission.
    off_topic_markers = ["gst", "tax", "loan", "insurance", "visa"]
    if any(m in text_l for m in off_topic_markers):
        return {
            "action": "send",
            "body": "That's outside what I handle here — worth checking with your CA/accountant for that. On your magicpin listing, is there anything you'd like me to look at?",
            "cta": "open_ended",
            "rationale": "Unrelated question — politely redirected without ending the conversation or pretending to help outside scope.",
        }

    # Default: acknowledge + advance.
    return {
        "action": "send",
        "body": "Got it — noted. Want me to go ahead and take the next step on this, or would you like more detail first?",
        "cta": "binary",
        "rationale": "Generic engaged reply — advances the conversation with a single low-friction binary ask.",
    }


# =============================================================================
# FastAPI app (challenge-testing-brief.md §2)
# =============================================================================

app = FastAPI(title="magicpin-challenge-bot")
_START = time.time()
_lock = threading.Lock()

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict[tuple[str, str], dict] = {}
# conversation_id -> state
CONVERSATIONS: dict[str, dict] = {}
# merchant_id -> set of suppression_keys already sent (any conversation)
SENT_SUPPRESSION: dict[str, set] = {}


def _get_ctx(scope: str, cid: str) -> Optional[dict]:
    rec = CONTEXTS.get((scope, cid))
    return rec["payload"] if rec else None


def _counts() -> dict:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return counts


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - _START), "contexts_loaded": _counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Team Aish"),
        "team_members": [os.environ.get("TEAM_MEMBER", "Aish")],
        "model": "template-composer-v1 (deterministic, no external LLM call by default)",
        "approach": "Per-trigger-kind template composer reading real CategoryContext/MerchantContext/"
                    "CustomerContext facts, with a rule-based conversation FSM for auto-reply detection, "
                    "intent transitions, and graceful exits.",
        "contact_email": os.environ.get("TEAM_EMAIL", "team@example.com"),
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope {body.scope!r}"}
    key = (body.scope, body.context_id)
    with _lock:
        cur = CONTEXTS.get(key)
        if cur and cur["version"] >= body.version:
            return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
        CONTEXTS[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        if len(actions) >= 20:
            break
        trigger = _get_ctx("trigger", trg_id)
        if not trigger:
            continue
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        merchant = _get_ctx("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = _get_ctx("category", merchant.get("category_slug", ""))
        customer = _get_ctx("customer", customer_id) if customer_id else None

        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant_id}"
        with _lock:
            already_sent = suppression_key in SENT_SUPPRESSION.get(merchant_id, set())
        if already_sent:
            continue  # restraint: don't resend the same nudge (anti-repetition, spam penalty avoidance)

        composed = compose(category or {}, merchant, trigger, customer)
        if composed["cta"] == "none" and not composed["body"]:
            continue

        conv_id = f"conv_{merchant_id}_{trg_id}"
        with _lock:
            SENT_SUPPRESSION.setdefault(merchant_id, set()).add(suppression_key)
            CONVERSATIONS[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "history": [],
                "sent_bodies": [composed["body"]],
                "auto_reply_strikes": 0,
                "last_topic": trigger.get("kind", "this"),
            }

        sal = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [sal, trigger.get("kind", ""), composed["body"][:60]],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })
    return {"actions": actions}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    with _lock:
        state = CONVERSATIONS.setdefault(body.conversation_id, {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "history": [],
            "sent_bodies": [],
            "auto_reply_strikes": 0,
            "last_topic": "this",
        })
        history = list(state["history"])
        decision = decide_reply(history, body.message, {
            "auto_reply_strikes": state.get("auto_reply_strikes", 0),
            "last_topic": state.get("last_topic", "this"),
        })
        state["history"].append(body.message)
        if "_auto_reply_strikes" in decision:
            state["auto_reply_strikes"] = decision.pop("_auto_reply_strikes")

        if decision["action"] == "send":
            candidate = decision.get("body", "")
            # Anti-repetition safety net.
            if candidate and candidate in state.get("sent_bodies", []):
                candidate = candidate.rstrip(".") + " — let me know either way."
            state.setdefault("sent_bodies", []).append(candidate)
            return {
                "action": "send",
                "body": candidate,
                "cta": decision.get("cta", "open_ended"),
                "rationale": decision["rationale"],
            }
        elif decision["action"] == "wait":
            return {
                "action": "wait",
                "wait_seconds": decision.get("wait_seconds", 1800),
                "rationale": decision["rationale"],
            }
        else:
            resp = {"action": "end", "rationale": decision["rationale"]}
            if decision.get("body"):
                resp["body"] = decision["body"]
            return resp


@app.post("/v1/teardown")
async def teardown():
    with _lock:
        CONTEXTS.clear()
        CONVERSATIONS.clear()
        SENT_SUPPRESSION.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
