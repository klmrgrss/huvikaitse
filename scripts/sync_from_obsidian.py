#!/usr/bin/env python3
"""
EEEL Obsidian -> Astro turvaline opt-in ekspordiskript.
RANGE REEGEL: ekspordib AINULT failid, mille YAML frontmatteris on `veeb: "avalik"` (või `veeb: avalik`).
Sisemisi faile (`veeb: "sisemine"` või puuduva märgistusega) EI LOETA ega ekspordita kunagi.

Toetab:
- `Veeb/Avaleht.md` (`veeb_id: "avaleht"`) -> avalehe, teemalehe päise ja jaluse tekstid
- `Veeb/Pikk vaade 2035 — seiretöölaud.md` (`veeb_id: "pikk-vaade-2035"`) -> kogu seirelaua tekstid,
  võtmenäitajad, 7 sammu, tegevuste tabelid, dünaamiliste veergudega seiremõõdikud ja vabad lisasektsioonid
- `Teemad/*.md` -> avalik ülemine kiht (alumine sisemine töökiht lõigatakse välja)
- `Töörühmad/*.md` -> 3 töörühma kirjeldused
- `Jutupunktid/`, `Ringkirjad/`, `Sisendid/` -> ainult kui `veeb: "avalik"`
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

ALLOWED_DIRS = ["Teemad", "Töörühmad", "Jutupunktid", "Ringkirjad", "Sisendid", "Veeb"]
ALLOWED_VEEB_VALUES = {"avalik"}


def slugify(text: str) -> str:
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
            if v == "" or v == "[]":
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
    # Remove internal-only sections cleanly (loop to handle consecutive private headings)
    priv_re = re.compile(
        r"^##\s+(?:J[äa]rgmised\s+sammud|Tegevuslogi|Sise(?:mised\s+)?m[äa]rkmed)\b[\s\S]*?(?=^##\s+|\Z)",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    while priv_re.search(body):
        body = priv_re.sub("", body)
    # Remove any stray internal Riigikogu suund bullet in public body
    body = re.sub(r"^\s*-\s*\*\*Riigikogu suund:\*\*[^\n]*\n?", "", body, flags=re.M)
    # Remove leading Obsidian H1 + breadcrumb line (rendered cleanly by Astro header)
    body = re.sub(r"^\s*#\s+[^\n]+\n+", "", body, count=1)
    body = re.sub(r"^\s*←[^\n]+\n+", "", body, count=1)
    return body.strip()


def clean_wikilink_value(s: str) -> str:
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), s)


def extract_callout_lines(body: str, callout_type: str):
    pattern = rf"^>\s*\[!{callout_type}\][^\n]*\n((?:>[^\n]*\n?)*)"
    m = re.search(pattern, body, flags=re.M | re.I)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        line = re.sub(r"^>\s*", "", line).strip()
        if not line:
            continue
        if line.startswith("- "):
            items.append(line[2:].strip())
        else:
            items.append(line)
    return items


def resolve_wikilinks(md: str, route_map: dict, source_slug: str, backlinks_acc: dict) -> str:
    def repl(m):
        target_raw = m.group(1).strip()
        alias = m.group(2).strip() if m.group(2) else None
        target_name = target_raw.split("#")[0].strip()
        base_name = target_name.split("/")[-1].strip()
        label = alias or base_name

        # Never leak private attachments or Obsidian canvas/base files
        if re.search(r"\.(pdf|docx|pptx|asice|base|canvas)$", base_name, re.I):
            return f"*{label}*"

        target_info = route_map.get(target_name) or route_map.get(base_name)
        if target_info:
            if target_info["slug"] != source_slug and source_slug:
                backlinks_acc.setdefault(target_info["route"], set()).add(source_slug)
            return f"[{label}]({BASE_PATH}{target_info['route']})"

        if alias:
            return alias
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+", base_name):
            return re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", base_name)
        return label

    md = re.sub(r"!\[\[[^\]]+\]\]", "", md)
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", repl, md)


def convert_callouts_to_html(md: str) -> str:
    lines = md.splitlines()
    out = []
    i = 0
    type_labels = {
        "tip": ("Põhiseisukoht", "tip"),
        "warning": ("Oluline tähelepanek", "warning"),
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


def split_h2_sections(md: str):
    """Split markdown into ordered list of (heading, content) tuples for top-level ## sections."""
    sections = []
    current_heading = None
    current_lines = []
    for line in md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_lines).strip()))
    return sections


def parse_markdown_table(lines):
    rows = []
    headers = []
    header_done = False
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not header_done:
            headers = cells
            header_done = True
        elif all(re.match(r"^:?-{2,}:?$", c.replace(" ", "")) for c in cells):
            continue
        else:
            rows.append(cells)
    return headers, rows


def extract_first_table_and_intro(content: str):
    """Return (intro_text_before_table, headers, rows)."""
    intro_lines = []
    tbl_lines = []
    in_table = False
    table_done = False
    for line in content.splitlines():
        if not table_done and line.strip().startswith("|"):
            in_table = True
            tbl_lines.append(line)
        elif in_table:
            table_done = True
        elif not in_table:
            intro_lines.append(line)
    headers, rows = parse_markdown_table(tbl_lines)
    return "\n".join(intro_lines).strip(), headers, rows


def parse_site_config(meta: dict, body_resolved: str) -> dict:
    sections = {slugify(h): c for h, c in split_h2_sections(body_resolved)}
    return {
        "name": str(meta.get("veeb_nimi") or "EEELi seisukohad"),
        "description": str(
            meta.get("veeb_kirjeldus")
            or "Eesti Ehitusettevõtjate Liidu (EEEL) seisukohad ja „Ehituse pikk vaade 2035“ seire."
        ),
        "homeKicker": str(meta.get("kiker") or "Eesti Ehitusettevõtjate Liit"),
        "homeTitle": str(meta.get("pealkiri") or "EEELi seisukohad"),
        "homeLead": sections.get("sissejuhatus", ""),
        "homePikkVaadeKicker": str(meta.get("pikk_vaade_kiker") or "Ühiskondlik kokkulepe · 09.06.2021"),
        "homePikkVaadeTitle": str(
            meta.get("pikk_vaade_pealkiri") or "Ehituse pikk vaade 2035 — avalik seiretöölaud"
        ),
        "homePikkVaadeBody": sections.get("pikk-vaade-2035-plokk", ""),
        "homePikkVaadeGdpNote": str(
            meta.get("pikk_vaade_skp_markus")
            or "Samm 2 (kontratsüklilised riigi investeeringud) moodustab 70,9% kogu kokkuleppe majandusmõjust."
        ),
        "homeWorkgroupsMargin": sections.get("tookorraldus", ""),
        "homeTopicsMargin": sections.get("aluspohimote", ""),
        "topicsPageTitle": str(meta.get("teemade_leht_pealkiri") or "EEELi seisukohad"),
        "topicsPageLead": str(
            meta.get("teemade_leht_sissejuhatus")
            or "Valdkondlikud põhimõtted, lahendused ja taustamõju kolme töörühma ning „Ehituse pika vaate 2035“ lõikes."
        ),
        "footerOrg": str(meta.get("jalus_nimi") or "Eesti Ehitusettevõtjate Liit (EEEL)"),
        "footerWeb": str(meta.get("jalus_veeb") or "https://eeel.ee"),
        "footerEmail": str(meta.get("jalus_email") or "eeel@eeel.ee"),
        "updated": str(meta.get("uuendatud") or ""),
    }


def parse_pikk_vaade_dashboard(meta: dict, body_resolved: str) -> dict:
    """Parse ALL Pikk vaade 2035 dashboard content from Veeb/Pikk vaade 2035 — seiretöölaud.md."""
    sections = split_h2_sections(body_resolved)

    lead = ""
    raw_kpi_cards = []
    steps_summary = []
    action_headers = []
    actions = []
    actions_intro = ""
    kpi_headers = []
    kpis = []
    kpis_intro = ""
    extra_sections = []

    color_map = {
        "roheline": ("is-ok", "#2c6e49"),
        "ok": ("is-ok", "#2c6e49"),
        "kollane": ("is-warn", "#a66a1d"),
        "warn": ("is-warn", "#a66a1d"),
        "punane": ("is-alert", "#9e2a2b"),
        "alert": ("is-alert", "#9e2a2b"),
        "sinine": ("is-mid", "#3a6186"),
        "mid": ("is-mid", "#3a6186"),
        "neutraalne": ("", "#555555"),
    }

    for heading, content in sections:
        h_slug = slugify(heading)

        if h_slug == "sissejuhatus":
            lead = content
            continue

        if "votmenaitaj" in h_slug:
            _, _, rows = extract_first_table_and_intro(content)
            for r in rows:
                if len(r) >= 2:
                    raw_kpi_cards.append({
                        "value": r[0],
                        "label": r[1],
                        "caption": r[2] if len(r) > 2 else "",
                        "colorRaw": (r[3] if len(r) > 3 else "neutraalne").strip().lower(),
                        "barRaw": (r[4] if len(r) > 4 else "").strip(),
                    })
            continue

        if "suure-sammu" in h_slug or "koondtabel" in h_slug:
            _, _, rows = extract_first_table_and_intro(content)
            for r in rows:
                if len(r) >= 5:
                    code_m = re.match(r"^\*\*(?:Samm\s+|E)(\d+)\.\*\*\s*(.*)$", r[0])
                    if not code_m:
                        continue
                    step_num = int(code_m.group(1))
                    code = f"Samm {step_num}"
                    name = code_m.group(2).strip()
                    steps_summary.append({
                        "num": step_num,
                        "code": code,
                        "name": name,
                        "costMln": r[1].replace("**", ""),
                        "gdpMln": r[2].replace("**", ""),
                        "gdpShare": r[3].replace("**", ""),
                        "klimRaw": r[4],
                    })
            continue

        if "tegevuste" in h_slug:
            first_h3 = re.search(r"^###\s+", content, flags=re.M)
            if first_h3:
                actions_intro = content[: first_h3.start()].strip()
            goal_sections = re.finditer(
                r"### ((?:Eesmärk|Samm) (\d+): [^\n]+)\n((?:\|[^\n]+\n?)+)", content
            )
            for gm in goal_sections:
                goal_title = gm.group(1).strip()
                goal_num = int(gm.group(2))
                tbl_lines = gm.group(3).strip().splitlines()
                headers, rows = parse_markdown_table(tbl_lines)
                if headers and not action_headers:
                    action_headers = headers
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
                    extra_cells = r[7:] if len(r) > 7 else []

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
                        "goalCode": f"Samm {goal_num}",
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
                        "extraCells": extra_cells,
                    })
            continue

        if "seiremoodik" in h_slug or h_slug == "moodikud":
            intro, headers, rows = extract_first_table_and_intro(content)
            kpis_intro = intro
            kpi_headers = headers
            step_col_idx = 2
            for idx, h in enumerate(headers):
                if "samm" in slugify(h):
                    step_col_idx = idx
                    break
            for r in rows:
                if len(r) >= 2:
                    padded = r + [""] * max(0, len(headers) - len(r))
                    step_val = padded[step_col_idx] if step_col_idx < len(padded) else ""
                    step_m = re.search(r"(?:Samm\s+|E)(\d+)", step_val)
                    plain_search = " ".join(
                        re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", c) for c in padded
                    ).lower()
                    kpis.append({
                        "id": padded[0].replace("**", "").strip(),
                        "name": padded[1].strip() if len(padded) > 1 else "",
                        "step": step_val.replace("**", "").strip(),
                        "stepCode": f"Samm {step_m.group(1)}" if step_m else "Samm 1",
                        "baseline": padded[3].strip() if len(padded) > 3 else "",
                        "target": padded[4].strip() if len(padded) > 4 else "",
                        "source": padded[5].strip() if len(padded) > 5 else "",
                        "frequency": padded[6].strip() if len(padded) > 6 else "",
                        "cells": padded,
                        "searchText": plain_search,
                    })
            continue

        # Any other public ## section renders automatically as editorial prose
        if content:
            extra_sections.append({
                "id": h_slug,
                "heading": heading,
                "markdown": convert_callouts_to_html(content),
            })

    # Compute step counts for comparison bars
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

    # Resolve dynamic tokens inside Võtmenäitajad table
    regular_actions = [a for a in actions if not a["isNew"]]
    klim_done = sum(1 for a in regular_actions if a["klimCat"] == "valmis")
    total_regular = len(regular_actions)
    klim_done_pct = round((klim_done / max(total_regular, 1)) * 100)
    eeel_needs = sum(
        1 for a in regular_actions if a["eeelCat"] in ("kriitiline", "vastassuunaline")
    )
    tokens = {
        "{klim_valmis}": str(klim_done),
        "{kokku}": str(total_regular),
        "{klim_valmis_pct}": str(klim_done_pct),
        "{eeel_vajab}": str(eeel_needs),
        "{moodikute_arv}": str(len(kpis)),
    }

    def expand_tokens(s: str) -> str:
        for k, v in tokens.items():
            s = s.replace(k, v)
        return s

    kpi_cards = []
    for c in raw_kpi_cards:
        css_cls, bar_fill = color_map.get(c["colorRaw"], ("", "#555555"))
        bar_str = expand_tokens(c["barRaw"]).replace("%", "").replace(",", ".").strip()
        bar_pct = None
        if bar_str:
            try:
                bar_pct = max(0.0, min(100.0, float(bar_str)))
            except ValueError:
                bar_pct = None
        kpi_cards.append({
            "value": expand_tokens(c["value"]),
            "label": expand_tokens(c["label"]),
            "caption": expand_tokens(c["caption"]),
            "colorClass": css_cls,
            "barPct": bar_pct,
            "barColor": "#2c6e49" if (css_cls == "is-warn" and "{klim_valmis" in c["value"]) else bar_fill,
        })

    return {
        "kicker": str(meta.get("kiker") or "Avalik seiretöölaud · 09.06.2021 ühiskondlik kokkulepe"),
        "title": str(meta.get("pealkiri") or "Ehituse pikk vaade 2035"),
        "lead": lead,
        "kpiCards": kpi_cards,
        "stepsHeading": str(
            meta.get("sammude_pealkiri")
            or "7 suure sammu täitmine: KLIM vaheülevaade (20.01.2026) ja EEEL-i hinnang"
        ),
        "stepsRightLabel": str(meta.get("sammude_parem_silt") or "Paremal: 2022 lõpparuande SKP mõju"),
        "klimBarLabel": str(meta.get("klim_riba_silt") or "KLIM 2026"),
        "eeelBarLabel": str(meta.get("eeel_riba_silt") or "EEEL hinnang"),
        "actionsHeading": str(meta.get("tegevuste_pealkiri") or "Tegevuste täitmise ülevaade"),
        "actionsIntro": actions_intro,
        "actionHeaders": action_headers,
        "kpisHeading": str(meta.get("moodikute_pealkiri") or "Aastate lõikes jälgitavad seiremõõdikud"),
        "kpisIntro": kpis_intro,
        "kpiHeaders": kpi_headers,
        "steps": steps_summary,
        "actions": actions,
        "kpis": kpis,
        "extraSections": extra_sections,
        "updated": str(meta.get("uuendatud") or meta.get("kuupaev") or ""),
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
            h1_m = re.search(r"^#\s+(.+)$", body, re.M)
            display_title = str(meta.get("veeb_pealkiri") or "").strip() or (
                h1_m.group(1).strip() if h1_m else stem
            )
            slug = slugify(stem)
            veeb_id = str(meta.get("veeb_id") or "").strip().lower()

            if folder == "Veeb":
                if veeb_id == "avaleht":
                    kind = "veeb_avaleht"
                    route = "/"
                elif veeb_id == "pikk-vaade-2035":
                    kind = "veeb_pikk_vaade"
                    route = "/pikk-vaade-2035/"
                else:
                    kind = "analuus"
                    route = f"/analuusid/{slug}/"
            elif folder == "Teemad":
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
            info = {"slug": slug, "route": route, "title": display_title, "kind": kind}
            route_map[stem] = info
            route_map[f"{folder}/{stem}"] = info

    backlinks_acc = {}
    topics = []
    workgroups = []
    analyses = []
    riigikogu = None
    site_config = None
    pikk_vaade_dashboard = None

    slug_to_doc = {d["slug"]: d for d in docs}

    for d in docs:
        source_slug_for_bl = "" if d["kind"].startswith("veeb_") else d["slug"]
        resolved_md = resolve_wikilinks(d["rawBody"], route_map, source_slug_for_bl, backlinks_acc)

        if d["kind"] == "veeb_avaleht":
            site_config = parse_site_config(d["meta"], resolved_md)
            continue

        if d["kind"] == "veeb_pikk_vaade":
            pikk_vaade_dashboard = parse_pikk_vaade_dashboard(d["meta"], resolved_md)
            continue

        key_points = [clean_wikilink_value(b) for b in extract_callout_lines(d["rawBody"], "tip")]
        red_lines = [clean_wikilink_value(b) for b in extract_callout_lines(d["rawBody"], "warning")]
        summary = ""
        if key_points:
            summary = re.sub(r"\*\*([^*]+)\*\*", r"\1", key_points[0])
            summary = re.sub(r"\*([^*]+)\*", r"\1", summary)
        elif d["kind"] == "toogrupp":
            bq_m = re.search(r"^>\s*([^\n\[].+)$", resolved_md, re.M)
            if bq_m:
                summary = bq_m.group(1).strip()
        if not summary:
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

    # Attach backlinks (excluding workgroups & web config pages)
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
            if s in slug_to_doc and slug_to_doc[s]["kind"] not in ("toogrupp", "veeb_avaleht", "veeb_pikk_vaade")
        ]

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
        "site": site_config or parse_site_config({}, ""),
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
        f"{len(analyses)} analyses, siteConfig={bool(site_config)}, "
        f"pikkVaadeActions={len(pikk_vaade_dashboard['actions']) if pikk_vaade_dashboard else 0}, "
        f"pikkVaadeKPIs={len(pikk_vaade_dashboard['kpis']) if pikk_vaade_dashboard else 0}, "
        f"extraSections={len(pikk_vaade_dashboard['extraSections']) if pikk_vaade_dashboard else 0}"
    )


if __name__ == "__main__":
    main()
