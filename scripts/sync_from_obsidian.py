#!/usr/bin/env python3
"""
EEEL Obsidian -> Astro turvaline opt-in ekspordiskript.
Ekspordib AINULT failid, mille YAML frontmatteris on `veeb: "avalik"` (või `veeb: avalik`).
Puhastab Obsidiani sisemärkmed (%% ... %%), eemaldab katkised/privaatsed wikilingid
ja parsib automaatselt Pikk vaade 2035 auditi tabelid ja 24 seiremõõdikut töölaua jaoks.
"""
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

VAULT_DIR = os.environ.get("EEEL_VAULT_DIR", "/root/projects/eeel-obsidian")
OUT_JSON = os.environ.get(
    "EEEL_OUT_JSON", "/root/projects/huvikaitse/src/data/knowledge.json"
)
BASE_PATH = "/huvikaitse"

ALLOWED_DIRS = ["Teemad", "Töörühmad", "Jutupunktid", "Ringkirjad", "Sisendid"]
ALLOWED_VEEB_VALUES = {"avalik"}


def slugify(text: str) -> str:
    # Estonian diacritics friendly slug
    repl = {
        "õ": "o", "ä": "a", "ö": "o", "ü": "u",
        "Õ": "o", "Ä": "a", "Ö": "o", "Ü": "u",
        "š": "s", "ž": "z", "Š": "s", "Ž": "z",
        "—": "-", "–": "-",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", text)


def parse_frontmatter(raw: str):
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    fm_block = raw[4:end]
    body = raw[end + 5 :]

    meta = {}
    current_key = None
    for line in fm_block.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m_list = re.match(r"^\s*-\s+(.*)$", line)
        if m_list and current_key:
            val = m_list.group(1).strip().strip('"').strip("'")
            meta.setdefault(current_key, [])
            if isinstance(meta[current_key], list):
                meta[current_key].append(val)
            continue
        m_kv = re.match(r"^([a-zA-Z0-9_]+):\s*(.*)$", line)
        if m_kv:
            k = m_kv.group(1).strip()
            v = m_kv.group(2).strip()
            current_key = k
            if v == "":
                meta[k] = []
            elif v == "[]":
                meta[k] = []
            elif v.lower() == "true":
                meta[k] = True
            elif v.lower() == "false":
                meta[k] = False
            elif re.match(r"^\d+$", v):
                meta[k] = int(v)
            else:
                meta[k] = v.strip('"').strip("'")
    return meta, body


def strip_private_sections(body: str) -> str:
    # Remove Obsidian %% ... %% comments (multiline)
    body = re.sub(r"%%[\s\S]*?%%", "", body)
    # Remove any ## Sisemärkmed or ## Sisemised märkmed section until next ## heading
    body = re.sub(
        r"\n##\s+Sisem(?:ised\s+)?m[äa]rkmed[\s\S]*?(?=\n##\s+|\Z)",
        "\n",
        body,
        flags=re.IGNORECASE,
    )
    # Remove leading Obsidian H1 + breadcrumb line (rendered cleanly by Astro header)
    body = re.sub(r"^\s*#\s+[^\n]+\n+", "", body, count=1)
    body = re.sub(r"^\s*←[^\n]+\n+", "", body, count=1)
    return body.strip()


def clean_wikilink_value(s: str) -> str:
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), s)


def extract_callout_bullets(body: str, callout_type: str):
    pattern = rf"^>\s*\[!{callout_type}\][^\n]*\n((?:>[^\n]*\n?)*)"
    m = re.search(pattern, body, flags=re.M | re.I)
    if not m:
        return []
    bullets = []
    for line in m.group(1).splitlines():
        line = re.sub(r"^>\s*", "", line).strip()
        if line.startswith("- "):
            bullets.append(line[2:].strip())
    return bullets


def resolve_wikilinks(md: str, route_map: dict, source_slug: str, backlinks_acc: dict) -> str:
    def repl(m):
        target_raw = m.group(1).strip()
        alias = m.group(2).strip() if m.group(2) else None
        # Ignore headings/anchors inside target for lookup
        target_name = target_raw.split("#")[0].strip()
        label = alias or target_name

        # Never leak .pdf/.docx/.asice/.pptx private attachments unless explicitly published
        if re.search(r"\.(pdf|docx|pptx|asice|base|canvas)$", target_name, re.I):
            return f"*{label}*"

        if target_name in route_map:
            target_info = route_map[target_name]
            if target_info["slug"] != source_slug:
                backlinks_acc.setdefault(target_info["route"], set()).add(source_slug)
            return f"[{label}]({BASE_PATH}{target_info['route']})"

        # Private/unpublished target: render clean plain text (hide raw date-prefixed internal filenames)
        if alias:
            return alias
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+", target_name):
            # Remove date prefix from internal source note name
            return re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", target_name)
        return label

    # Strip embedded ![[...]] blocks cleanly
    md = re.sub(r"!\[\[[^\]]+\]\]", "", md)
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", repl, md)


def convert_callouts_to_html(md: str) -> str:
    """Convert Obsidian > [!type] Title callouts into fenced HTML divs while keeping inner markdown."""
    lines = md.splitlines()
    out = []
    i = 0
    type_labels = {
        "tip": ("Kehtivad jutupunktid", "tip"),
        "warning": ("Tähelepanu / mida me ei toeta", "warning"),
        "important": ("Oluline", "important"),
        "info": ("Info", "info"),
        "note": ("Märkus", "note"),
        "abstract": ("Kokkuvõte", "info"),
        "question": ("Küsimus", "note"),
    }
    while i < len(lines):
        m = re.match(r"^>\s*\[!([a-zA-Z0-9_-]+)\]\s*(.*)$", lines[i])
        if m:
            ctype = m.group(1).lower()
            default_title, css_cls = type_labels.get(ctype, (ctype.capitalize(), "note"))
            title = m.group(2).strip() or default_title
            block = []
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                block.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            inner = "\n".join(block).strip()
            out.append(f'<div class="callout callout-{css_cls}">')
            out.append(f'<div class="callout-title">{title}</div>')
            out.append("")
            out.append(inner)
            out.append("")
            out.append("</div>")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def parse_markdown_table(lines):
    rows = []
    headers = []
    for idx, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if idx == 0:
            headers = cells
        elif idx == 1 and all(re.match(r"^:?-{2,}:?$", c.replace(" ", "")) for c in cells):
            continue
        else:
            rows.append(cells)
    return headers, rows


def parse_pikk_vaade_dashboard(body_resolved: str):
    """Parse the 7 steps, 39 actions (with Märkused!), and 24 KPIs directly from the analysis Markdown."""
    steps_summary = []
    actions = []
    kpis = []

    # 1. Summary table in section 1 (E1..E7)
    sec1 = re.search(r"## 1\.[\s\S]*?(?=\n## 2\.)", body_resolved)
    if sec1:
        tbl_lines = [l for l in sec1.group(0).splitlines() if l.strip().startswith("|")]
        _, rows = parse_markdown_table(tbl_lines)
        for r in rows:
            if len(r) >= 5 and r[0].startswith("**E"):
                code_m = re.search(r"E(\d)", r[0])
                code = f"E{code_m.group(1)}" if code_m else r[0]
                name = re.sub(r"\*\*E\d+\.\*\*\s*", "", r[0]).strip()
                steps_summary.append({
                    "code": code,
                    "name": name,
                    "costMln": r[1].replace("**", ""),
                    "gdpMln": r[2].replace("**", ""),
                    "gdpShare": r[3].replace("**", ""),
                    "klimRaw": r[4],
                })

    # 2. All 7 goal tables in section 3
    goal_sections = re.finditer(
        r"### (Eesmärk (\d+): [^\n]+)\n((?:\|[^\n]+\n)+)", body_resolved
    )
    for gm in goal_sections:
        goal_title = gm.group(1).strip()
        goal_num = int(gm.group(2))
        tbl_lines = gm.group(3).strip().splitlines()
        headers, rows = parse_markdown_table(tbl_lines)
        for r in rows:
            if len(r) < 6:
                continue
            nr = r[0].replace("**", "").replace("*", "").strip()
            action_text = r[1].replace("**", "").strip()
            owner = r[2].replace("**", "").strip()
            klim_status = r[3].replace("**", "").strip()
            eeel_text = r[4].strip()
            related = r[5].strip()
            note = r[6].strip() if len(r) > 6 else ""

            # Classify KLIM status
            kl_low = klim_status.lower()
            if "valmis" in kl_low:
                klim_cat = "valmis"
            elif "töös" in kl_low:
                klim_cat = "toos"
            elif "alustatud" in kl_low:
                klim_cat = "alustatud"
            elif "seisab" in kl_low:
                klim_cat = "seisab"
            else:
                klim_cat = "uus"

            # Classify EEEL critical status
            if "⛔" in eeel_text:
                eeel_cat = "vastassuunaline"
            elif "🔴" in eeel_text:
                eeel_cat = "kriitiline"
            elif "🟢/🟡" in eeel_text:
                eeel_cat = "osaline"
            elif "🟢" in eeel_text:
                eeel_cat = "edasiminek"
            else:
                eeel_cat = "osaline"

            actions.append({
                "goalNum": goal_num,
                "goalCode": f"E{goal_num}",
                "goalTitle": goal_title,
                "nr": nr,
                "isNew": nr.lower() == "uus",
                "title": action_text,
                "owner": owner,
                "klimStatus": klim_status,
                "klimCat": klim_cat,
                "eeelAssessment": eeel_text,
                "eeelCat": eeel_cat,
                "related": related,
                "note": note,
            })

    # 3. 24 KPIs in section 4.2
    sec42 = re.search(r"### 4\.2\.[\s\S]*?(?=\n## 5\.)", body_resolved)
    if sec42:
        tbl_lines = [l for l in sec42.group(0).splitlines() if l.strip().startswith("|")]
        _, rows = parse_markdown_table(tbl_lines)
        for r in rows:
            if len(r) >= 7:
                step_m = re.search(r"E(\d)", r[2])
                kpis.append({
                    "id": r[0].replace("**", "").strip(),
                    "name": r[1].strip(),
                    "step": r[2].replace("**", "").strip(),
                    "stepCode": f"E{step_m.group(1)}" if step_m else "E1",
                    "baseline": r[3].strip(),
                    "target": r[4].strip(),
                    "source": r[5].strip(),
                    "frequency": r[6].strip(),
                })

    # Compute exact counts per step for charts
    for s in steps_summary:
        code = s["code"]
        step_actions = [a for a in actions if a["goalCode"] == code and not a["isNew"]]
        s["total"] = len(step_actions)
        s["klimCounts"] = {
            "valmis": sum(1 for a in step_actions if a["klimCat"] == "valmis"),
            "toos": sum(1 for a in step_actions if a["klimCat"] == "toos"),
            "alustatud": sum(1 for a in step_actions if a["klimCat"] == "alustatud"),
            "seisab": sum(1 for a in step_actions if a["klimCat"] == "seisab"),
        }
        s["eeelCounts"] = {
            "edasiminek": sum(1 for a in step_actions if a["eeelCat"] == "edasiminek"),
            "osaline": sum(1 for a in step_actions if a["eeelCat"] == "osaline"),
            "kriitiline": sum(1 for a in step_actions if a["eeelCat"] in ("kriitiline", "vastassuunaline")),
            "vastassuunaline": sum(1 for a in step_actions if a["eeelCat"] == "vastassuunaline"),
        }

    return {
        "steps": steps_summary,
        "actions": actions,
        "kpis": kpis,
    }


def main():
    docs = []
    route_map = {}

    for folder in ALLOWED_DIRS:
        folder_path = os.path.join(VAULT_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        for fname in sorted(os.listdir(folder_path)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
            meta, body = parse_frontmatter(raw)
            veeb = str(meta.get("veeb", "sisemine")).strip().lower()
            if veeb not in ALLOWED_VEEB_VALUES:
                continue

            stem = fname[:-3]
            title = str(meta.get("veeb_pealkiri") or "").strip() or stem
            # Clean leading H1 if available for prettier title
            h1_m = re.search(r"^#\s+(.+)$", body, re.M)
            display_title = str(meta.get("veeb_pealkiri") or "").strip() or (
                h1_m.group(1).strip() if h1_m else stem
            )
            slug = slugify(stem)

            # Determine web route
            if folder == "Teemad":
                kind = "teema"
                route = f"/teemad/{slug}/"
            elif folder == "Töörühmad":
                kind = "toogrupp"
                route = f"/teemad/?toogrupp={slug}"
            elif stem == "Ootused järgmisele Riigikogu koosseisule":
                kind = "riigikogu"
                route = "/riigikogu/"
            else:
                kind = "analuus"
                route = f"/analuusid/{slug}/"

            doc = {
                "stem": stem,
                "title": display_title,
                "slug": slug,
                "folder": folder,
                "kind": kind,
                "route": route,
                "meta": meta,
                "rawBody": strip_private_sections(body),
            }
            docs.append(doc)
            route_map[stem] = {"slug": slug, "route": route, "title": display_title, "kind": kind}

    # Special route alias for Pikk vaade dashboard
    backlinks_acc = {}
    topics = []
    workgroups = []
    analyses = []
    riigikogu = None
    pikk_vaade_dashboard = None

    # Build lookup by slug for backlink labels
    slug_to_doc = {d["slug"]: d for d in docs}

    for d in docs:
        resolved_md = resolve_wikilinks(d["rawBody"], route_map, d["slug"], backlinks_acc)
        # Extract clean key points & red lines BEFORE converting callouts to HTML
        key_points = [clean_wikilink_value(b) for b in extract_callout_bullets(d["rawBody"], "tip")]
        red_lines = [clean_wikilink_value(b) for b in extract_callout_bullets(d["rawBody"], "warning")]
        # Summary: first key point or first non-heading paragraph
        summary = ""
        if key_points:
            summary = re.sub(r"\*\*([^*]+)\*\*", r"\1", key_points[0])
            summary = re.sub(r"\*([^*]+)\*", r"\1", summary)
        else:
            for para in resolved_md.split("\n\n"):
                p = para.strip()
                if p and not p.startswith(("#", ">", "|", "-", "<")):
                    summary = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", p)
                    summary = re.sub(r"\*\*([^*]+)\*\*", r"\1", summary)
                    break

        html_ready_md = convert_callouts_to_html(resolved_md)
        meta = d["meta"]
        entry = {
            "slug": d["slug"],
            "stem": d["stem"],
            "title": d["title"],
            "kind": d["kind"],
            "route": d["route"],
            "workgroup": str(meta.get("toogrupp_nimi") or ""),
            "workgroupSlug": slugify(str(meta.get("toogrupp_nimi") or "")),
            "status": str(meta.get("staatus") or ""),
            "priority": str(meta.get("prioriteet") or ""),
            "audiences": meta.get("sihtruhm") if isinstance(meta.get("sihtruhm"), list) else [],
            "riigikoguTopic": bool(meta.get("riigikogu_teema", False)),
            "riigikoguPillars": meta.get("riigikogu_suund") if isinstance(meta.get("riigikogu_suund"), list) else [],
            "updated": str(meta.get("uuendatud") or meta.get("kuupaev") or ""),
            "order": int(meta.get("jarjekord", 99)) if str(meta.get("jarjekord", "")).isdigit() else 99,
            "summary": summary[:260] + ("…" if len(summary) > 260 else ""),
            "keyPoints": key_points,
            "redLines": red_lines,
            "markdown": html_ready_md,
            "backlinks": [],
        }

        if d["kind"] == "teema":
            topics.append(entry)
        elif d["kind"] == "toogrupp":
            workgroups.append(entry)
        elif d["kind"] == "riigikogu":
            riigikogu = entry
        else:
            analyses.append(entry)

    # Parse Pikk vaade 2035 structured dashboard tables directly from source file
    # (even when the full internal analysis memo itself has veeb: "sisemine")
    pv_source = os.path.join(
        VAULT_DIR,
        "Jutupunktid",
        "2026-11-23 Analüüs — Ehituse pikk vaade 2035 täitmise kriitiline audit ja seiremõõdikud.md",
    )
    if os.path.isfile(pv_source):
        with open(pv_source, "r", encoding="utf-8") as f:
            pv_raw = f.read()
        pv_meta, pv_body = parse_frontmatter(pv_raw)
        pv_resolved = resolve_wikilinks(strip_private_sections(pv_body), route_map, "pikk-vaade-2035", {})
        pikk_vaade_dashboard = parse_pikk_vaade_dashboard(pv_resolved)
        pv_analysis_pub = next((a for a in analyses if "pikk-vaade-2035" in a["slug"]), None)
        pikk_vaade_dashboard["analysisRoute"] = pv_analysis_pub["route"] if pv_analysis_pub else None
        pikk_vaade_dashboard["analysisTitle"] = pv_analysis_pub["title"] if pv_analysis_pub else None
        pikk_vaade_dashboard["updated"] = str(pv_meta.get("kuupaev") or pv_meta.get("uuendatud") or "")

    # Attach backlinks
    all_entries = topics + analyses + ([riigikogu] if riigikogu else [])
    for e in all_entries:
        bl_slugs = sorted(backlinks_acc.get(e["route"], set()))
        e["backlinks"] = [
            {
                "title": slug_to_doc[s]["title"],
                "route": slug_to_doc[s]["route"],
                "kind": slug_to_doc[s]["kind"],
            }
            for s in bl_slugs
            if s in slug_to_doc and slug_to_doc[s]["kind"] != "toogrupp"
        ]

    # Sort topics: overarching Pikk vaade first, then High priority, then alphabetical
    prio_rank = {"Kõrge": 1, "Keskmine": 2, "Madal": 3}
    topics.sort(
        key=lambda t: (
            0 if "pikk-vaade-2035" in t["slug"] else 1,
            prio_rank.get(t["priority"], 9),
            t["title"],
        )
    )
    workgroups.sort(key=lambda w: w["order"])
    analyses.sort(key=lambda a: a["updated"], reverse=True)

    now = datetime.now(timezone.utc)
    payload = {
        "syncedAtIso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "syncedAtDisplay": now.strftime("%d.%m.%Y"),
        "basePath": BASE_PATH,
        "counts": {
            "topics": len(topics),
            "workgroups": len(workgroups),
            "analyses": len(analyses),
            "kpis": len(pikk_vaade_dashboard["kpis"]) if pikk_vaade_dashboard else 0,
        },
        "workgroups": workgroups,
        "topics": topics,
        "analyses": analyses,
        "riigikogu": riigikogu,
        "pikkVaade": pikk_vaade_dashboard,
    }

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(
        f"Synced {len(topics)} topics, {len(workgroups)} workgroups, "
        f"{len(analyses)} analyses, riigikogu={bool(riigikogu)}, "
        f"pikkVaadeActions={len(pikk_vaade_dashboard['actions']) if pikk_vaade_dashboard else 0}, "
        f"pikkVaadeKPIs={len(pikk_vaade_dashboard['kpis']) if pikk_vaade_dashboard else 0}"
    )


if __name__ == "__main__":
    main()
