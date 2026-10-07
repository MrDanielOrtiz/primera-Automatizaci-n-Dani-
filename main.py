from __future__ import annotations

import csv, json, os, re, smtplib, sys, zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
REPORTS = ROOT / "reports"
RAW.mkdir(parents=True, exist_ok=True); PROCESSED.mkdir(parents=True, exist_ok=True); REPORTS.mkdir(parents=True, exist_ok=True)

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text(encoding="utf-8"))

@dataclass
class Record:
    record_id: str
    kind: str
    title: str
    url: str
    official_source: str
    publication_date: str = ""
    register: str = ""
    court: str = ""
    matter: str = ""
    epoch: str = ""
    text: str = ""
    source_page: str = ""
    relevance_score: int = 0
    priority: str = "BAJA"
    area: str = ""
    why: str = ""

def die(msg: str):
    raise RuntimeError("RADAR FAIL-FAST: " + msg)

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def latest_fridays(n):
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(CFG["timezone"])).date()
    except Exception:
        today = date.today()
    d = today - timedelta(days=(today.weekday() - 4) % 7)
    return [d - timedelta(days=7*i) for i in range(n)]

def requested_edition():
    value = os.getenv("TARGET_EDITION_DATE", "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        die("TARGET_EDITION_DATE inválida: " + value + ". Use YYYY-MM-DD.")

def is_scheduled_poll():
    return os.getenv("RADAR_MODE", "manual").lower() == "scheduled"

def seen_path():
    return ROOT / "seen.json"

def load_seen():
    p = seen_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else data.get("processed_editions", []))
    except Exception:
        return set()

def mark_seen(target):
    seen = load_seen()
    seen.add(target.isoformat())
    seen_path().write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

def same_host(url, allowed):
    return urlparse(url).netloc in {urlparse(x).netloc for x in allowed}

def click_week(page, target: date) -> bool:
    # The official site is intentionally interacted with as a user-facing application.
    # We do not hard-code undocumented API endpoints.
    candidates = [
        target.strftime("%d/%m/%Y"),
        target.strftime("%d-%m-%Y"),
        target.strftime("%Y-%m-%d"),
        str(target.day),
    ]
    for value in candidates:
        try:
            loc = page.get_by_text(value, exact=True).first
            if loc.count():
                loc.click()
                return True
        except Exception:
            pass
    # Calendar controls often expose date in title/aria-label/data-date.
    for sel in ["[data-date]", "[aria-label]", "[title]"]:
        try:
            els = page.locator(sel)
            for i in range(min(els.count(), 1000)):
                e = els.nth(i)
                blob = " ".join(filter(None, [e.get_attribute("data-date"), e.get_attribute("aria-label"), e.get_attribute("title")]))
                if target.isoformat() in blob or target.strftime("%d/%m/%Y") in blob:
                    e.click(); return True
        except Exception:
            pass
    return False

def collect_week(page, target: date, strict=True):
    urls = [CFG["weekly_page"], CFG["agreements_page"]]
    records = []
    visited = set()
    for start_url in urls:
        page.goto(start_url, wait_until="domcontentloaded", timeout=CFG["request_timeout_seconds"]*1000)
        page.wait_for_timeout(1200)
        if not click_week(page, target):
            # Try year/month controls and inspect all visible links as a last documented-UI route.
            try:
                page.get_by_text(str(target.year), exact=True).last.click()
                page.wait_for_timeout(400)
                page.get_by_text(target.strftime("%B").capitalize(), exact=True).last.click()
                page.wait_for_timeout(400)
            except Exception:
                pass
            if not click_week(page, target):
                if strict:
                    raise RuntimeError("La edición " + target.isoformat() + " no está disponible en la interfaz oficial.")
                return []
        for selector in ["input[type=checkbox]", "input[type=radio]"]:
            try:
                for i in range(page.locator(selector).count()):
                    el = page.locator(selector).nth(i)
                    if el.is_visible() and not el.is_checked():
                        el.check()
            except Exception:
                pass
        for label in ["Buscar", "Consultar"]:
            try:
                page.get_by_role("button", name=re.compile(label, re.I)).click()
                page.wait_for_timeout(1500)
                break
            except Exception:
                pass
        # Paginate through result pages using visible pagination controls.
        for _ in range(100):
            html = page.content()
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(page.url, a["href"])
                txt = clean(a.get_text(" ", strip=True))
                if not same_host(href, urls):
                    continue
                if re.search(r"/detalle/|/tesis/|/ejecutoria/|/voto/|/acuerdo/", href, re.I):
                    key = (href, txt)
                    if key in visited: continue
                    visited.add(key)
                    records.append({"url": href, "title": txt, "source_page": page.url})
            nxt = None
            for role in ["Siguiente", "Siguiente página", "Next"]:
                try:
                    x = page.get_by_role("link", name=re.compile(role, re.I)).last
                    if x.count() and x.is_visible() and x.get_attribute("aria-disabled") != "true":
                        nxt = x; break
                except Exception: pass
            if not nxt:
                break
            try:
                before = page.url
                nxt.click(); page.wait_for_timeout(1000)
                if page.url == before and "disabled" in (nxt.get_attribute("class") or "").lower():
                    break
            except Exception:
                break
    if not records:
        if strict:
            die("No fue posible recuperar resultados oficiales para la semana " + target.isoformat() + ".")
        return []
    return records

def parse_detail(page, item, target):
    page.goto(item["url"], wait_until="domcontentloaded", timeout=CFG["request_timeout_seconds"]*1000)
    page.wait_for_timeout(300)
    soup = BeautifulSoup(page.content(), "lxml")
    text = clean(soup.get_text(" ", strip=True))
    title = clean(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else item["title"]
    body = {}
    labels = ["Registro digital", "Órgano", "Materia", "Época", "Fecha", "Tipo de criterio"]
    for label in labels:
        node = soup.find(string=re.compile(re.escape(label), re.I))
        if node:
            parent = node.parent
            body[label] = clean(parent.parent.get_text(" ", strip=True) if parent and parent.parent else str(node))
    register = ""
    m = re.search(r"Registro digital\s*[:#]?\s*(\d{5,})", text, re.I)
    if m: register = m.group(1)
    kind = "Precedente" if "ejecutoria" in item["url"] or "precedente" in title.lower() else ("Acuerdo" if "acuerdo" in item["url"] else "Tesis")
    return Record(
        record_id=register or item["url"],
        kind=kind, title=title, url=item["url"], official_source=item["url"],
        publication_date=target.isoformat(), register=register,
        court=body.get("Órgano",""), matter=body.get("Materia",""),
        epoch=body.get("Época",""), text=text[:30000], source_page=item["source_page"]
    )

def score_record(r: Record):
    blob = " ".join([r.title, r.text, r.court, r.matter]).lower()
    p1 = PROFILE["priority"]["P1"].lower()
    p2 = PROFILE["priority"]["P2"].lower()
    p3 = PROFILE["priority"]["P3"].lower()
    score = 0; area = "Otros"
    if any(k.lower() in blob for k in PROFILE["topics"]): score += 30
    if any(k.lower() in blob for k in PROFILE["authorities"]): score += 10
    if any(k.lower() in blob for k in PROFILE["frameworks"]): score += 10
    if any(k.lower() in blob for k in PROFILE["transversal"]): score += CFG["score"]["transversal"]
    if "pleno" in blob: score += CFG["score"]["plenary"]
    if any(k in blob for k in ["cambia de criterio","abandona el criterio","supera","contradicción","nueva jurisprudencia"]): score += CFG["score"]["line_change"]
    if any(k in blob for k in ["aduaner","comercio exterior","importación","exportación","pama","immex","cuota compensatoria","clasificación arancelaria","iva/ieps"]):
        score += CFG["score"]["p1"]; area = p1
    elif any(k in blob for k in ["fiscal","impuesto","iva","ieps","código fiscal","devolución","compensación"]):
        score += CFG["score"]["p2"]; area = p2
    elif any(k in blob for k in ["administrativ","autoridad administrativa","procedimiento administrativo"]):
        score += CFG["score"]["p3"]; area = p3
    elif "amparo" in blob: score += CFG["score"]["p4"]; area = PROFILE["priority"]["P4"]
    elif any(k in blob for k in ["constitucional","derechos humanos"]): score += CFG["score"]["p5"]; area = PROFILE["priority"]["P5"]
    score = min(score, 100)
    r.relevance_score = score; r.area = area
    r.priority = "URGENTE" if score >= CFG["score"]["urgent"] else "ALTA" if score >= CFG["score"]["high"] else "MEDIA" if score >= CFG["score"]["medium"] else "BAJA"
    if r.priority in ("URGENTE","ALTA"):
        r.why = f"Puede impactar directamente {area.lower()} y/o alguno de los temas monitorizados por el perfil."
    return r

def archive_source(target, pages, records):
    folder = RAW / target.isoformat(); folder.mkdir(exist_ok=True)
    manifest = {"edition_date": target.isoformat(), "official_pages": pages, "record_count": len(records), "records": []}
    session = requests.Session(); session.headers["User-Agent"] = "RadarJurisprudencial/1.0"
    for i, rec in enumerate(records, 1):
        p = folder / f"{i:05d}.html"
        try:
            rr = session.get(rec.url, timeout=CFG["request_timeout_seconds"])
            rr.raise_for_status()
            p.write_bytes(rr.content)
        except Exception:
            p.write_text(rec.text, encoding="utf-8")
        manifest["records"].append(asdict(rec))
    (folder/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = RAW / f"weekly_source_{target.isoformat()}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in folder.rglob("*"):
            if f.is_file(): z.write(f, f.relative_to(folder.parent))
    return zip_path

def save_inventory(target, records):
    js = PROCESSED / f"inventory_{target.isoformat()}.json"
    js.write_text(json.dumps([asdict(x) for x in records], ensure_ascii=False, indent=2), encoding="utf-8")
    with (PROCESSED / f"inventory_{target.isoformat()}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w=csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys())); w.writeheader(); w.writerows(asdict(x) for x in records)
    return js

def make_pdf(target, records):
    path = REPORTS / f"Radar_Jurisprudencial_{target.isoformat()}.pdf"
    styles=getSampleStyleSheet(); styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="CenterTitle", parent=styles["Title"], alignment=TA_CENTER))
    doc=SimpleDocTemplate(str(path), pagesize=A4, rightMargin=15*mm,leftMargin=15*mm,topMargin=14*mm,bottomMargin=14*mm)
    story=[Paragraph("Radar Jurisprudencial", styles["CenterTitle"]),
           Paragraph(f"Daniel Ortiz · Aguirre y Asociados · Edición {target.strftime('%d/%m/%Y')}", styles["Normal"]),
           Spacer(1,5*mm),
           Paragraph(f"Universo procesado: {len(records)} criterios/registros.", styles["Normal"]),
           Paragraph("Prioridad: P1 Comercio exterior/aduanero · P2 Fiscal · P3 Administrativo · P4 Amparo · P5 Constitucional/DH.", styles["Small"]),
           Spacer(1,5*mm), Paragraph("RESUMEN EJECUTIVO", styles["Heading2"])]
    top=sorted(records,key=lambda x:x.relevance_score,reverse=True)
    for r in top[:8]:
        if r.priority in ("URGENTE","ALTA","MEDIA"):
            story += [Paragraph(f"<b>{r.priority} · {r.relevance_score}/100 · {r.title}</b>", styles["BodyText"]),
                      Paragraph(f"Registro: {r.register or 'No verificado'} · Órgano: {r.court or 'No verificado'}", styles["Small"]),
                      Paragraph(r.why or "Relevancia menor o transversal.", styles["Small"]), Spacer(1,2*mm)]
    story += [PageBreak(), Paragraph("CRITERIOS PRIORITARIOS", styles["Heading2"])]
    for r in top[:20]:
        if r.priority == "BAJA": continue
        story += [Paragraph(f"<b>{r.priority} · {r.area} · {r.title}</b>", styles["Heading3"]),
                  Paragraph(f"Registro digital: {r.register or 'No verificado'} · Tipo: {r.kind} · Fecha: {r.publication_date}", styles["Small"]),
                  Paragraph(f"Fuente oficial: {r.url}", styles["Small"]),
                  Paragraph((r.text[:1000] + ("…" if len(r.text)>1000 else "")), styles["Small"]), Spacer(1,3*mm)]
    story += [PageBreak(), Paragraph("ÍNDICE COMPLETO DEL UNIVERSO", styles["Heading2"])]
    data=[["Prioridad","Área","Registro","Tipo","Título"]]
    for r in records:
        data.append([r.priority,r.area,r.register or "—",r.kind,r.title[:100]])
    t=Table(data,colWidths=[18*mm,30*mm,23*mm,20*mm,80*mm],repeatRows=1)
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.25,None),("FONTSIZE",(0,0),(-1,-1),6),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [t, Spacer(1,4*mm), Paragraph("Herramienta de apoyo. Verifique siempre la fuente oficial.", styles["Small"])]
    doc.build(story)
    return path

def send_email(target, pdf, source_zip, records):
    sender=os.getenv("RADAR_EMAIL_FROM"); password=os.getenv("RADAR_EMAIL_PASSWORD")
    if not sender or not password:
        print("Correo omitido: faltan RADAR_EMAIL_FROM/RADAR_EMAIL_PASSWORD.")
        return
    top=[r for r in sorted(records,key=lambda x:x.relevance_score,reverse=True) if r.priority in ("URGENTE","ALTA")][:7]
    msg=EmailMessage(); msg["Subject"]=f"Radar Jurisprudencial | {target:%d/%m/%Y} | P1/P2"
    msg["From"]=sender; msg["To"]=PROFILE["email"]
    bullets="\n".join(f"- {r.priority}: {r.title} ({r.register or 'registro no verificado'})" for r in top)
    msg.set_content((bullets or "Sin alertas prioritarias esta semana.")+"\n\nSe adjuntan el PDF y el paquete completo de fuente oficial.")
    for path,ctype in [(pdf,"application/pdf"),(source_zip,"application/zip")]:
        msg.add_attachment(path.read_bytes(), maintype=ctype.split("/")[0], subtype=ctype.split("/")[1], filename=path.name)
    with smtplib.SMTP(CFG["email"]["host"],CFG["email"]["port"]) as s:
        s.starttls(); s.login(sender,password); s.send_message(msg)

def main():
    target=None; raw_items=None
    explicit_target = requested_edition()
    scheduled = is_scheduled_poll()
    if explicit_target:
        candidates = [explicit_target]
    elif scheduled:
        candidates = latest_fridays(1)
    else:
        candidates = latest_fridays(CFG["lookback_fridays"])

    if scheduled and candidates and candidates[0].isoformat() in load_seen():
        print("OK: la edición " + candidates[0].isoformat() + " ya fue procesada; no se vuelve a enviar.")
        return
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(locale="es-MX")
        for friday in candidates:
            try:
                raw_items=collect_week(page, friday, strict=not scheduled)
                if raw_items:
                    target=friday; break
            except Exception as e:
                print("No disponible " + str(friday) + ": " + str(e))
        if scheduled and not target:
            print("Aún no aparece la edición semanal oficial; el siguiente disparo programado volverá a comprobarla.")
            return
        if not target or not raw_items: die("No se pudo verificar una edición semanal oficial.")
        records=[]
        for item in raw_items:
            try: records.append(parse_detail(page,item,target))
            except Exception as e: print("Detalle omitido con error verificable:",item["url"],e)
        browser.close()
    if not records: die("La edición oficial no produjo registros procesables; se detiene para evitar huecos silenciosos.")
    records=[score_record(r) for r in records]
    source_zip=archive_source(target, [CFG["weekly_page"],CFG["agreements_page"]], records)
    save_inventory(target, records)
    pdf=make_pdf(target, records)
    send_email(target,pdf,source_zip,records)
    mark_seen(target)
    print(f"OK {target.isoformat()} registros={len(records)} pdf={pdf} source={source_zip}")

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(str(exc), file=sys.stderr); sys.exit(1)
