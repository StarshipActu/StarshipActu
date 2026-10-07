#!/usr/bin/env python3
"""
StarshipActu : génération automatique des visuels « retard / fermeture de route »
et « fermeture de plage » à partir de https://www.starbase.texas.gov/beach-road-access

Installation (une seule fois) :
    pip install pillow requests tzdata

Utilisation :
    python starshipactu_auto.py              -> tourne en boucle (vérifie toutes les N minutes)
    python starshipactu_auto.py --once       -> une seule vérification (pour le planificateur)
    python starshipactu_auto.py --once --force   -> régénère même si rien n'a changé
    python starshipactu_auto.py --debug      -> affiche ce que le script a compris de la page
    python starshipactu_auto.py --html page.html --once --inclure-termine   -> test sur un fichier local
"""
import argparse, hashlib, json, os, random, re, sys, time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

# ============================ RÉGLAGES ============================
URL = "https://www.starbase.texas.gov/beach-road-access"
LOGO = "logo.jpg"                  # votre logo StarshipActu, à placer à côté du script
SORTIE = "sortie"                  # dossier où sont enregistrées les images
ROUTE = "Texas State Highway 4"    # le site ne nomme pas la route : à adapter si besoin
LIEU_PLAGE = "Boca Chica Beach"
SOURCE = "starbase.texas.gov/beach-road-access"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")  # URL d'un webhook Discord (optionnel) : voir le guide GitHub Actions
VERIF_TOUTES_LES_MIN = 20          # intervalle de vérification en mode boucle
# ==================================================================

CT, PARIS = ZoneInfo("America/Chicago"), ZoneInfo("Europe/Paris")
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MK = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TRADUCTIONS = {
    "production to masseys": "Production vers Massey",
    "production to massey": "Production vers Massey",
    "production to port": "Production vers le port",
}
DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)\s*(?:to|-|–|until)\s*"
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)", re.I)


# ----------------------------- lecture de la page -----------------------------
class _Texte(HTMLParser):
    def __init__(self):
        super().__init__()
        self.p, self.skip = [], 0

    def handle_starttag(self, t, a):
        if t in ("script", "style", "noscript"):
            self.skip += 1
        if t in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.p.append(" ")

    def handle_endtag(self, t):
        if t in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, d):
        if not self.skip:
            self.p.append(d)


def html_vers_texte(html):
    p = _Texte()
    p.feed(html)
    return re.sub(r"\s+", " ", "".join(p.p)).strip()


def telecharger():
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0 (StarshipActu)"})
    r.raise_for_status()
    return r.text


def h24(h, ap):
    return int(h) % 12 + (12 if ap.upper() == "PM" else 0)


def analyser(texte, inclure_termine=False):
    """Retourne (fenetres, statut_plage)."""
    bas = texte.lower()
    maintenant = datetime.now(CT)
    fenetres = []
    for m in DATE_RE.finditer(texte):
        a, b = MK.index(m[1][:3].lower()) if m[1][:3].lower() in MK else -1, \
               MK.index(m[6][:3].lower()) if m[6][:3].lower() in MK else -1
        if a < 0 or b < 0:
            continue
        y = maintenant.year
        d1, d2 = int(m[2]), int(m[7])
        debut = datetime(y, a + 1, d1, h24(m[3], m[5]), int(m[4]), tzinfo=CT)
        fin = datetime(y if (b, d2) >= (a, d1) else y + 1, b + 1, d2, h24(m[8], m[10]), int(m[9]), tzinfo=CT)
        if fin < maintenant and not inclure_termine:
            continue
        avant = texte[max(0, m.start() - 200):m.start()]
        i = avant.lower().rfind("description:")
        motif = re.sub(r"\s*Date:\s*$", "", avant[i + 12:].strip(), flags=re.I) if i >= 0 else ""
        motif = motif or "Fenêtre prévue"
        motif = TRADUCTIONS.get(motif.lower(), motif)
        # section (route ou plage) : dernier titre rencontré avant la date
        pos_route = max(bas.rfind(k, 0, m.start()) for k in ("road delay", "road closure", "road update"))
        pos_plage = max(bas.rfind(k, 0, m.start()) for k in ("beach closure", "beach access status", "beach status"))
        section = "plage" if pos_plage > pos_route else "route"
        contexte = bas[max(0, m.start() - 300):m.start()]
        fermeture = contexte.rfind("closure") > contexte.rfind("delay")
        mp, fp = debut.astimezone(PARIS), fin.astimezone(PARIS)
        meme = (debut.month, debut.day) == (fin.month, fin.day)
        memep = (mp.month, mp.day) == (fp.month, fp.day)
        local = (f"{debut.day} {MOIS[debut.month-1]} {debut.year} · {debut:%Hh%M} → "
                 f"{'' if meme else f'{fin.day} {MOIS[fin.month-1]} · '}{fin:%Hh%M} ({debut.tzname()})")
        paris = (f"{mp.day} {MOIS[mp.month-1]} {mp.year} · {mp:%Hh%M} → "
                 f"{'' if memep else f'{fp.day} {MOIS[fp.month-1]} · '}{fp:%Hh%M}")
        fenetres.append({"section": section, "fermeture": fermeture, "motif": motif, "local": local, "paris": paris})
    s = re.search(r"beach\s+is\s+(open|closed)", texte, re.I)
    statut = s[1].lower() if s else "inconnu"
    return fenetres, statut


# ------------------------------- dessin des images -------------------------------
def police(taille, gras=True):
    noms = (["arialbd.ttf", "Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "DejaVuSans-Bold.ttf"] if gras else
            ["arial.ttf", "Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"])
    for n in noms:
        try:
            return ImageFont.truetype(n, taille)
        except OSError:
            pass
    return ImageFont.load_default(taille)


def lerp(stops, t):
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return stops[-1][1]


def fond(teinte):
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    ciel = [(0, (7, 12, 36, 255)), (.55, (13, 31, 92, 255)), (1, (23, 51, 138, 255))]
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp(ciel, y / H))
    rnd = random.Random(7)
    etoiles = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    e = ImageDraw.Draw(etoiles)
    for _ in range(70):
        x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.choice((1, 1, 2, 2, 3))
        e.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randrange(110, 230)))
    img = Image.alpha_composite(img, etoiles)
    lav = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    t = ImageDraw.Draw(lav)
    for x in range(W):
        t.line([(x, 0), (x, H)], fill=lerp(teinte, x / W))
    return Image.alpha_composite(img, lav)


TEINTE_ROUTE = [(0, (45, 90, 210, 77)), (.2, (45, 90, 210, 36)), (.4, (255, 255, 255, 61)),
                (.6, (255, 255, 255, 61)), (.8, (214, 40, 57, 36)), (1, (214, 40, 57, 77))]
TEINTE_PLAGE = [(0, (30, 150, 200, 77)), (.2, (30, 150, 200, 36)), (.4, (255, 255, 255, 61)),
                (.6, (255, 255, 255, 61)), (.8, (240, 200, 120, 41)), (1, (240, 200, 120, 77))]
VERT = (98, 198, 60)


def barriere(img, x0, y0):
    d = ImageDraw.Draw(img)
    for lx in (26, 110):
        d.rounded_rectangle([x0 + lx, y0 + 26, x0 + lx + 14, y0 + 130], 3, fill=(170, 182, 194))
    for py in (32, 76):
        pl = Image.new("RGBA", (134, 30), (242, 245, 250, 255))
        pd = ImageDraw.Draw(pl)
        for sx in range(-30, 140, 32):
            pd.polygon([(sx, 30), (sx + 16, 30), (sx + 46, 0), (sx + 30, 0)], fill=(214, 40, 57, 255))
        masque = Image.new("L", (134, 30), 0)
        ImageDraw.Draw(masque).rounded_rectangle([0, 0, 133, 29], 5, fill=255)
        img.paste(pl, (x0 + 8, y0 + py), masque)
        d.rounded_rectangle([x0 + 8, y0 + py, x0 + 142, y0 + py + 30], 5, outline=(11, 18, 54), width=3)


def logo_rond(img):
    try:
        lg = Image.open(LOGO).convert("RGB")
    except OSError:
        return
    c = min(lg.size)
    lg = lg.crop(((lg.width - c) // 2, (lg.height - c) // 2, (lg.width + c) // 2, (lg.height + c) // 2)).resize((190, 190))
    m = Image.new("L", (190, 190), 0)
    ImageDraw.Draw(m).ellipse([0, 0, 189, 189], fill=255)
    x, y = 1920 - 56 - 190, 1080 - 48 - 190
    img.paste(lg, (x, y), m)
    ImageDraw.Draw(img).ellipse([x - 3, y - 3, x + 192, y + 192], outline=VERT, width=6)
    f = police(44)
    d = ImageDraw.Draw(img)
    d.text((x - 22, y + 95), "StarshipActu", font=f, fill="white", anchor="rm")


def texte_ajuste(d, xy, txt, taille, largeur_max, **kw):
    gras = kw.pop("gras", True)
    f = police(taille, gras)
    while d.textlength(txt, font=f) > largeur_max and taille > 24:
        taille -= 2
        f = police(taille, gras)
    d.text(xy, txt, font=f, **kw)


def carte(titre, sous_titre, lieu, fenetres, teinte, chemin):
    img = fond(teinte)
    barriere(img, 64, 44)
    d = ImageDraw.Draw(img)
    texte_ajuste(d, (260, 40), titre.upper(), 136, 1600, fill="white")
    d.text((264, 186), sous_titre.upper(), font=police(44, False), fill=VERT)
    for x in range(64, 1764):
        a = round(255 * (1 - (x - 64) / 1700))
        d.line([(x, 262), (x, 265)], fill=(*VERT, a))
    texte_ajuste(d, (64, 330), lieu.upper(), 104, 1700, fill="white")
    d.text((64, 490), "Fenêtres prévues", font=police(36, False), fill=(207, 227, 245))
    lignes = []
    for w in fenetres:
        lignes += [(w["motif"], w["local"]), ("Heure de Paris", w["paris"])]
    pas = min(80, 380 // max(len(lignes), 1))
    for i, (lab, val) in enumerate(lignes):
        y = 548 + i * pas
        texte_ajuste(d, (64, y), lab, int(pas * .6), 620, fill=VERT)
        texte_ajuste(d, (704, y), val, int(pas * .68), 1152, fill="white", gras=False)
    d.text((64, 1028), SOURCE, font=police(36, False), fill=(207, 227, 245), anchor="ls")
    logo_rond(img)
    img.convert("RGB").save(chemin)


# --------------------------------- envoi / boucle ---------------------------------
def envoyer_discord(chemin, message):
    if not DISCORD_WEBHOOK:
        return
    with open(chemin, "rb") as f:
        requests.post(DISCORD_WEBHOOK, data={"content": message}, files={"file": (Path(chemin).name, f)}, timeout=30)


def verifier(args):
    html = Path(args.html).read_text(encoding="utf-8") if args.html else telecharger()
    texte = html_vers_texte(html)
    fenetres, statut = analyser(texte, args.inclure_termine)
    if args.debug:
        print("Statut plage :", statut)
        print(json.dumps(fenetres, ensure_ascii=False, indent=2))
        print("Texte lu (début) :", texte[:600])
    signature = hashlib.sha1(json.dumps([fenetres, statut], sort_keys=True).encode()).hexdigest()
    sortie = Path(SORTIE)
    sortie.mkdir(exist_ok=True)
    etat = sortie / "etat.json"
    ancien = json.loads(etat.read_text())["sig"] if etat.exists() else None
    if signature == ancien and not args.force:
        print(datetime.now().strftime("%H:%M"), "Rien de nouveau.")
        return
    etat.write_text(json.dumps({"sig": signature}))
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    creees = []
    groupes = {}
    for w in fenetres:
        groupes.setdefault((w["section"], w["fermeture"]), []).append(w)
    for (section, fermeture), ws in groupes.items():
        if section == "route":
            titre = "Fermeture de route" if fermeture else "Retard routier"
            nom, lieu, teinte = ("fermeture-route" if fermeture else "retard-routier"), ROUTE, TEINTE_ROUTE
            sous = "Ville de Starbase, TX · Accès plage et route"
        else:
            titre, nom, lieu, teinte = "Fermeture de plage", "fermeture-plage", LIEU_PLAGE, TEINTE_PLAGE
            sous = "Ville de Starbase, TX · Accès plage"
        chemin = sortie / f"{nom}_{horodatage}.png"
        carte(titre, sous, lieu, ws, teinte, chemin)
        creees.append((chemin, titre))
    if statut == "closed" and not any(s == "plage" for s, _ in groupes):
        chemin = sortie / f"fermeture-plage_{horodatage}.png"
        carte("Fermeture de plage", "Ville de Starbase, TX · Accès plage", LIEU_PLAGE,
              [{"motif": "Statut", "local": "Plage fermée", "paris": "Voir le site de la ville"}], TEINTE_PLAGE, chemin)
        creees.append((chemin, "Fermeture de plage"))
    if not creees:
        print("Aucune fermeture ni retard en cours (plage :", statut + ").")
    for chemin, titre in creees:
        print("Image créée :", chemin)
        envoyer_discord(chemin, f"{titre} : mise à jour")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--html", help="lire un fichier HTML local au lieu du site (pour tester)")
    ap.add_argument("--inclure-termine", action="store_true", help="garder aussi les fenêtres déjà passées")
    args = ap.parse_args()
    while True:
        try:
            verifier(args)
        except Exception as e:  # réseau coupé, page modifiée, etc. : on réessaie au prochain tour
            print("Erreur :", e, file=sys.stderr)
        if args.once or VERIF_TOUTES_LES_MIN <= 0:
            break
        time.sleep(VERIF_TOUTES_LES_MIN * 60)


if __name__ == "__main__":
    main()
