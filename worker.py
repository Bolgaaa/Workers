"""
9look Worker v2 — Sans queue, recherche directe
Playwright dans le thread principal, Flask dans un thread séparé.
"""

import os
import time
import queue
import threading
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import requests
import json

load_dotenv()

RAILWAY_URL   = os.getenv("RAILWAY_URL", "").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "change_this")
PORT          = int(os.getenv("PORT", 10000))
HEADLESS      = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
COOKIE_FILE   = "/tmp/intelscry_cookies.json"
INTELSCRY_URL = "https://dashboard.intelscry.cc/search"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("9look-worker")

app = Flask(__name__)

worker_ready = False
current_job  = None
job_queue    = queue.Queue()

def check_secret():
    if request.headers.get("X-Worker-Secret", "") != WORKER_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ready": worker_ready, "busy": current_job is not None})

@app.route("/session", methods=["POST"])
def set_session():
    err = check_secret()
    if err: return err
    data = request.json
    if not data or "cookies" not in data:
        return jsonify({"error": "Missing cookies"}), 400
    with open(COOKIE_FILE, "w") as f:
        json.dump(data["cookies"], f)
    log.info("Session cookies saved (%d cookies)", len(data["cookies"]))
    return jsonify({"ok": True, "count": len(data["cookies"])})

@app.route("/search", methods=["POST"])
def search():
    err = check_secret()
    if err: return err
    if not worker_ready:
        return jsonify({"error": "Worker not ready"}), 503
    data   = request.json
    query  = data.get("query", "").strip()
    job_id = data.get("job_id", "")
    if not query:
        return jsonify({"error": "Query vide"}), 400
    position = job_queue.qsize()
    log.info(f"Job {job_id} — {query} (queue: {position})")
    job_queue.put({"query": query, "job_id": job_id})
    return jsonify({"status": "accepted", "job_id": job_id})

@app.route("/searcher", methods=["POST"])
def searcher_route():
    err = check_secret()
    if err: return err
    if not worker_ready:
        return jsonify({"error": "Worker not ready"}), 503
    data       = request.json
    query      = data.get("query", "").strip()
    quick      = data.get("quickSearch", "").strip()
    criteria   = data.get("criteria", [])
    wildcard   = data.get("wildcard", False)
    job_id     = data.get("job_id", "")
    if not query and not quick and not criteria:
        return jsonify({"error": "Requete vide"}), 400
    position = job_queue.qsize()
    log.info(f"Searcher job {job_id} — {query} (position queue: {position})")
    job_queue.put({"type": "searcher", "query": query, "quickSearch": quick, "criteria": criteria, "wildcard": wildcard, "job_id": job_id})
    return jsonify({"status": "accepted", "job_id": job_id, "position": position})

@app.route("/status", methods=["GET"])
def status():
    err = check_secret()
    if err: return err
    return jsonify({"ready": worker_ready, "busy": current_job is not None})

def decode_cf_email(encoded):
    """Decode Cloudflare email obfuscation"""
    try:
        import binascii
        r = int(encoded[:2], 16)
        return ''.join(chr(int(encoded[i:i+2], 16) ^ r) for i in range(2, len(encoded), 2))
    except:
        return '[email]'

def process_search(page, query, job_id):
    global current_job
    current_job = job_id
    sources = []
    error = None
    try:
        log.info(f"  -> Recherche: {query}")

        page.goto(INTELSCRY_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("input", timeout=15000)

        inp = page.locator("input").first
        inp.click()
        inp.fill(query)
        inp.press("Enter")

        log.info(f"  -> Attente spinner IntelScry...")
        try:
            page.wait_for_selector("text=Interrogation", timeout=10000)
            log.info(f"  -> Recherche en cours...")
            page.wait_for_selector("text=Interrogation", state="hidden", timeout=90000)
            log.info(f"  -> Terminee!")
        except PlaywrightTimeout:
            log.warning("  Spinner non detecte, on continue...")

        time.sleep(1)

        # Compter les dossiers
        folder_count = page.locator("div.space-y-1 button").count()
        log.info(f"  -> {folder_count} dossiers trouves")

        for btn_idx in range(folder_count):
            try:
                btn = page.locator("div.space-y-1 button").nth(btn_idx)
                btn_text = btn.inner_text()
                lines = btn_text.strip().splitlines()
                lines = [l.strip() for l in lines if l.strip()]

                source_name = ''
                source_count = 0
                for line in lines:
                    if line.isdigit():
                        source_count = int(line)
                    elif len(line) > 1:
                        source_name = line

                if not source_name:
                    continue

                log.info(f"  -> Ouverture: {source_name} ({source_count} entrees)")
                btn.click()
                time.sleep(3)

                # ── Extraire via regex sur le HTML brut ───────────────────────
                import re
                html = page.content()

                # Decoder les emails Cloudflare obfusques
                def decode_cf(m):
                    return decode_cf_email(m.group(1))

                # Splitter par entree (#1/N, #2/N, etc.)
                entry_blocks = re.split(r'#\d+/\d+', html)
                entries = []

                for block in entry_blocks[1:]:  # skip premier bloc vide
                    entry = {}
                    # Pattern: title="LABEL">LABEL</span><span...font-mono...>VALEUR</span>
                    fields = re.findall(
                        r'title="([^"]+)"[^<]*</span><span[^>]*font-mono[^>]*>(.*?)</span>',
                        block
                    )
                    for label, value in fields:
                        label = label.strip()
                        # Decoder emails CF obfusques
                        value = re.sub(r'data-cfemail="([a-f0-9]+)"', decode_cf, value)
                        # Nettoyer HTML
                        value = re.sub(r'<[^>]+>', '', value).strip()
                        value = value.replace('&#160;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                        if label and value and len(value) < 5000:
                            entry[label] = value

                    if entry:
                        entries.append(entry)

                log.info(f"    -> {len(entries)} entrees pour {source_name}")
                sources.append({
                    'sourceName': source_name,
                    'count': source_count or len(entries),
                    'entries': entries
                })

            except Exception as ex:
                log.warning(f"  Erreur dossier {btn_idx}: {ex}")
                continue

        if not sources:
            log.warning("  Aucun dossier, fallback")
            sources = [{'sourceName': 'IntelScry', 'count': 0, 'entries': []}]

        total = sum(s.get('count', 0) for s in sources)
        log.info(f"  Total: {total} resultats dans {len(sources)} sources")

    except Exception as ex:
        error = str(ex)
        log.error(f"  Erreur: {ex}")
    finally:
        current_job = None

    if RAILWAY_URL:
        try:
            total = sum(s.get('count', 0) for s in sources)
            requests.post(
                f"{RAILWAY_URL}/api/worker/result",
                json={
                    "job_id":   job_id,
                    "query":    query,
                    "sources":  sources,
                    "results":  sources,
                    "count":    total,
                    "error":    error,
                    "timestamp": datetime.now().isoformat()
                },
                headers={"X-Worker-Secret": WORKER_SECRET},
                timeout=15
            )
            log.info(f"  Envoye a Railway OK")
        except Exception as ex:
            log.error(f"  Erreur envoi: {ex}")


def start_ngrok():
    if not NGROK_TOKEN:
        log.warning("⚠ NGROK_AUTH_TOKEN manquant")
        return None
        # Fermer les     try:
                time.sleep(2)
    except:
        pass
        log.info(f"🔗     if RAILWAY_URL:
        try:
            r = requests.post(f"{RAILWAY_URL}/api/worker/register", json={"worker_url": url}, headers={"X-Worker-Secret": WORKER_SECRET}, timeout=10)
            if r.status_code == 200:
                log.info("✅ Enregistré sur Railway")
        except Exception as e:
            log.error(f"✗ Register: {e}")
    return url

def process_searcher(page, job_id, quick_search, criteria, wildcard):
    global current_job
    current_job = job_id
    sources = []
    error = None
    try:
        import re as re2
        log.info(f"  -> Searcher: quick={quick_search} criteria={criteria}")
        page.goto("https://dashboard.intelscry.cc/searcher", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if quick_search:
            quick_inp = page.locator("input[placeholder*='libre'], input[placeholder*='Recherche libre']").first
            quick_inp.click()
            quick_inp.fill(quick_search)
            log.info(f"  -> Quick search: {quick_search}")
        else:
            # Remplir critères un par un
            # Les inputs de critères ont placeholder "Ex: ..."
            for crit_idx, crit in enumerate(criteria):
                val = crit.get("value", "").strip()
                crit_type = crit.get("type", "") or crit.get("label", "")
                if not val or not crit_type:
                    continue

                label_map = {
                    'Nom': 'Nom', 'Prenom': 'Prénom', 'Prénom': 'Prénom',
                    'Date': 'Date de naissance', 'DateNaissance': 'Date de naissance',
                    'CodePostal': 'Code Postal', 'Code Postal': 'Code Postal',
                    'Rue': 'Rue', 'Ville': 'Ville', 'NIR': 'NIR',
                    'Telephone': 'Téléphone', 'Téléphone': 'Téléphone',
                    'Email': 'Email', 'Plaque': 'Plaque', 'VIN': 'VIN', 'IBAN': 'IBAN'
                }
                target = label_map.get(crit_type, crit_type)
                log.info(f"    -> Critere [{crit_idx}]: {target} = {val}")

                # Cliquer "Ajouter un critère"
                page.locator("button:has-text('Ajouter un crit')").first.click()
                time.sleep(0.6)

                # Cliquer le type dans le picker
                clicked = False
                for btn in page.locator("button").all():
                    try:
                        if not btn.is_visible(): continue
                        txt = btn.inner_text().strip()
                        if txt.lower() == target.lower():
                            btn.click()
                            clicked = True
                            log.info(f"    -> Clique type: '{txt}'")
                            break
                    except: pass

                if not clicked:
                    log.warning(f"    -> ECHEC clic type {target}, Escape")
                    try: page.keyboard.press("Escape")
                    except: pass
                    continue

                time.sleep(0.5)

                # Remplir le Nth input[placeholder^="Ex:"] (index = crit_idx)
                # Chaque critère ajouté crée un nouvel input "Ex: ..."
                filled = False
                for attempt in range(10):
                    ex_inputs = page.locator("input[placeholder^='Ex']").all()
                    log.info(f"    -> {len(ex_inputs)} inputs Ex: trouves")
                    if len(ex_inputs) > crit_idx:
                        target_inp = ex_inputs[crit_idx]
                        try:
                            target_inp.click()
                            target_inp.fill(val)
                            time.sleep(0.2)
                            actual = target_inp.input_value()
                            log.info(f"    -> Rempli [{crit_idx}]: '{actual}'")
                            filled = True
                            break
                        except Exception as fe:
                            log.warning(f"    -> Fill error: {fe}")
                    time.sleep(0.4)

                if not filled:
                    log.warning(f"    -> ECHEC remplissage {target}")

        # Wildcard
        if wildcard:
            try:
                page.locator("button:has-text('Wildcard')").first.click()
                log.info("  -> Wildcard ON")
            except: pass

        # Lancer la recherche
        page.locator("button.btn-primary, button:has-text('Lancer la recherche')").first.click()
        log.info("  -> Recherche lancee...")

        # Attendre les resultats (dossiers)
        for _ in range(60):
            time.sleep(1)
            try:
                cnt = page.locator("button.w-full.flex.items-center.gap-3").count()
                if cnt > 0:
                    log.info(f"  -> {cnt} dossiers apparus!")
                    break
            except: pass

        time.sleep(1)

        # Helper: decode Cloudflare obfuscated emails
        def decode_cf(encoded):
            try:
                r = int(encoded[:2], 16)
                return "".join(chr(int(encoded[i:i+2], 16) ^ r) for i in range(2, len(encoded), 2))
            except: return "[email]"

        # Helper: decode CF emails
        def decode_html(h):
            h = re2.sub(r'data-cfemail="([a-f0-9]+)"',
                        lambda m: decode_cf(m.group(1)), h)
            h = re2.sub(r'<[^>]+>', '', h)
            return h.replace('&#160;', ' ').replace('&amp;', '&').strip()

        def extract_entries_from_html(html_content):
            entry_blocks = re2.split(r'#\d+/\d+', html_content)
            entries = []
            for block in entry_blocks[1:]:
                entry = {}
                pairs = re2.findall(
                    r'title="([^"]+)"[^>]*>[^<]*</span>\s*<span[^>]*break-all[^>]*>(.*?)</span>',
                    block, re2.DOTALL
                )
                for k, v in pairs:
                    k = k.strip()
                    v = decode_html(v)
                    if k and v and len(v) < 3000:
                        entry[k] = v
                if entry:
                    entries.append(entry)
            return entries

        def get_btn_info(btn):
            try:
                raw = re2.sub(r'<[^>]+>', ' ', btn.inner_html())
                raw = re2.sub(r'\s+', ' ', raw).strip()
                nums = re2.findall(r'\b(\d+)\b', raw)
                words = re2.sub(r'\b\d+\b', '', raw).strip()
                words = re2.sub(r'\s+', ' ', words).strip()
                return words[:60] or "Source", int(nums[-1]) if nums else 0
            except:
                return "Source", 0

        folder_sel_l1 = "button.w-full.flex.items-center.gap-3"  # Dossiers niveau 1
        folder_sel_l2 = "button.w-full.flex.items-center.gap-2"  # Sous-dossiers
        folder_sel = folder_sel_l1  # Pour compatibilite

        # Etape 1: identifier et ouvrir les dossiers niveau 1
        top_btns_init = page.locator(folder_sel_l1).all()
        top_count = len(top_btns_init)
        log.info(f"  -> {top_count} dossiers niveau 1")

        top_info = []
        for tb in top_btns_init:
            n, c = get_btn_info(tb)
            top_info.append((n, c))
            log.info(f"    L1: {n} ({c})")

        # Ouvrir chaque dossier L1 un par un
        for ti in range(top_count):
            try:
                top_name, top_cnt = top_info[ti]
                if top_cnt == 0:
                    continue

                # Re-query et click
                all_now = page.locator(folder_sel).all()
                # Le bouton L1 est toujours aux premiers indices
                all_now[ti].click()
                time.sleep(0.8)

                # Attendre que les sous-dossiers (gap-2) apparaissent
                # "Ajouter un critere" = 1 gap-2 toujours present, on attend > 1
                for wait_i in range(20):
                    time.sleep(0.5)
                    n_all_gap2 = page.locator(folder_sel_l2).count()
                    if n_all_gap2 > 1:  # Plus que juste "Ajouter un critere"
                        time.sleep(0.3)
                        break
                n_subs = page.locator(folder_sel_l2).count() - 1  # -1 pour "Ajouter un critere"
                log.info(f"  -> {top_name}: {n_subs} sous-dossiers (gap-2)")

                src_entry = {"sourceName": top_name, "count": top_cnt, "subSources": [], "entries": []}

                if n_subs > 0:
                    # Les sous-dossiers sont entre la position (ti+1) et (ti+1+n_subs)
                    # car les L1 sont 0..top_count-1, et apres click ti les subs s'insèrent apres ti
                    sub_start = ti + 1
                    sub_end = sub_start + n_subs

                    # Collecter les sous-dossiers via gap-2 selector
                    # Filtrer uniquement ceux avec un count > 0 (exclut "Ajouter un critère")
                    # Et prendre seulement les N premiers qui correspondent a ce L1
                    all_gap2 = page.locator(folder_sel_l2).all()
                    log.info(f"    {len(all_gap2)} boutons gap-2 total")
                    sub_info = []
                    for si, sb in enumerate(all_gap2):
                        sn, sc = get_btn_info(sb)
                        log.info(f"    gap2[{si}]: {sn} ({sc})")
                        if sc > 0 and len(sub_info) < n_subs:
                            sub_info.append((si, sn, sc))
                    log.info(f"    Subs retenus: {len(sub_info)} / {n_subs}")

                    # IntelScry garde les entrees precedentes visibles
                    # -> tracker combien on a deja extrait pour prendre seulement les nouvelles
                    extracted_so_far = 0
                    for sub_idx, (orig_si, sub_name, sub_cnt) in enumerate(sub_info):
                        try:
                            # Re-query gap-2 buttons (index stable car pas de nouveaux boutons gap-2)
                            sub_btns_now = page.locator(folder_sel_l2).all()
                            log.info(f"      [{sub_idx}] gap-2[{orig_si}]/{len(sub_btns_now)}: {sub_name} ({sub_cnt})")
                            if orig_si >= len(sub_btns_now):
                                log.warning(f"      HORS RANGE")
                                continue

                            sub_btns_now[orig_si].scroll_into_view_if_needed()
                            time.sleep(0.3)
                            sub_btns_now[orig_si].click()
                            
                            # Attendre que les entrees apparaissent
                            loaded = False
                            for w in range(15):
                                time.sleep(1)
                                try:
                                    body_text = page.inner_text("body")
                                    if "#1/" in body_text:
                                        loaded = True
                                        log.info(f"      Entrees chargees en {w+1}s")
                                        break
                                except: pass
                            if not loaded:
                                log.warning(f"      TIMEOUT {sub_name} - tentative quand meme")
                                time.sleep(1)

                            # Extraire MAINTENANT - seulement les nouvelles entrees
                            html_now = page.content()
                            all_entries_now = extract_entries_from_html(html_now)
                            # Les nouvelles entrees = total - deja extraites
                            new_entries = all_entries_now[extracted_so_far:]
                            log.info(f"      -> total={len(all_entries_now)} nouveau={len(new_entries)} (attendu {sub_cnt})")
                            extracted_so_far += len(new_entries)

                            src_entry["subSources"].append({
                                "sourceName": sub_name,
                                "count": sub_cnt,
                                "entries": new_entries[:300]
                            })
                            src_entry["entries"].extend(new_entries[:300])

                        except Exception as se:
                            log.warning(f"    Sub {sub_name}: {se}")

                    # Fermer L1 pour la source suivante
                    try:
                        page.locator(folder_sel_l1).all()[ti].click()
                        time.sleep(0.3)
                    except: pass

                else:
                    # Pas de sous-dossiers
                    entries = extract_entries_from_html(page.content())
                    src_entry["entries"] = entries[:500]
                    log.info(f"    -> {len(entries)} entrees directes")
                    try:
                        page.locator(folder_sel_l1).all()[ti].click()
                        time.sleep(0.5)
                    except: pass

                sources.append(src_entry)
            except Exception as ex:
                log.warning(f"  L1 {ti}: {ex}")

        total = sum(len(s.get("entries", [])) for s in sources)
        log.info(f"  Total: {total} entrees dans {len(sources)} sources")

    except Exception as ex:
        error = str(ex)
        log.error(f"  Erreur globale: {ex}")
    finally:
        current_job = None

    if RAILWAY_URL:
        try:
            requests.post(
                f"{RAILWAY_URL}/api/worker/result",
                json={"job_id": job_id, "sources": sources,
                      "count": sum(len(s.get("entries",[])) for s in sources),
                      "error": error, "timestamp": datetime.now().isoformat()},
                headers={"X-Worker-Secret": WORKER_SECRET}, timeout=15
            )
            log.info("  -> Envoye OK")
        except Exception as ex:
            log.error(f"  Envoi: {ex}")


def main():
    global worker_ready
    log.info("")
    log.info("╔══════════════════════════════════════╗")
    log.info("║      9look Worker v2.0               ║")
    log.info("║  Direct · Sans queue · Rapide        ║")
    log.info("╚══════════════════════════════════════╝")
    log.info("")

    log.info("🌐 Lancement de Chrome...")
    pw = sync_playwright().start()
    user_data_dir = os.path.join(os.path.expanduser("~"), ".9look-chrome-profile")
    os.makedirs(user_data_dir, exist_ok=True)

    browser = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=False,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
        no_viewport=True,
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    page = browser.new_page()
    log.info(f"  → Ouverture de IntelScry...")
    page.goto(INTELSCRY_URL, wait_until="domcontentloaded")

    log.info("")
    log.info("=" * 50)
    log.info("  ⚠  CONNECTE-TOI À INTELSCRY DANS CHROME")
    log.info("  Appuie sur ENTRÉE quand c'est fait...")
    log.info("=" * 50)
    input()

    worker_ready = True
    log.info("✅ Worker prêt !")

    
    # Flask dans un thread séparé
    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False), daemon=True)
    t.start()
    log.info(f"🚀 En attente de recherches...")
    log.info("")
    log.info("  Tu peux minimiser Chrome et ce terminal.")
    log.info("  Ne les FERME PAS.")
    log.info("")

    # Boucle principale Playwright
    while True:
        try:
            job = job_queue.get(timeout=1)
            if job.get("type") == "searcher":
                process_searcher(page, job["job_id"], job.get("quickSearch",""), job.get("criteria",[]), job.get("wildcard",False))
            else:
                process_search(page, job["query"], job["job_id"])
        except queue.Empty:
            pass
        except Exception as ex:
            log.error(f"Erreur: {ex}")

if __name__ == "__main__":
    main()
