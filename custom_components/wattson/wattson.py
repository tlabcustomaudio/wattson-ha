#!/usr/bin/env python3
"""Wattson v0 — l'investigatore dei consumi per Home Assistant.

Legge SOLO il contatore generale (qualsiasi sensore di potenza in W o kW) e:
  - riconosce i gradini di carico e li accoppia in cicli (acceso → spento),
  - raggruppa i cicli in impronte ("+1,8 kW per ~7 min") da nominare,
  - pubblica un semaforo per i carichi pesanti (sensor.wattson_semaforo):
      go   = via libera, wait = poco margine (con "aspetta N min" se lo sa),
      stop = oltre la potenza impegnata,
  - notifica solo i passaggi a/da stop, con chi è acceso (stile CapillAir),
  - ogni mattina misura il carico di base notturno e avvisa se sale,
  - trova da solo prese/interruttori con misura (device_class power) e li usa:
    elenco "misurati" + "non misurato", e nome automatico delle impronte
    quando un gradino sul generale coincide con un sotto-contatore,
  - osserva lo stato di dispositivi senza misura (stampanti, ecc.): se un
    gradino coincide due volte col cambio di stato dello stesso dispositivo,
    l'impronta prende il suo nome ("Stampante: running"),
  - registra ogni lettura del generale (samples-AAAA-MM-GG.csv) e ogni cambio
    di stato dei dispositivi osservati, per ricostruire le impronte dopo,
  - modalità apprendimento guidata: "bianco" con quasi tutto spento, poi un
    dispositivo alla volta → profili di consumo per stato (profiles.json).

Uso:
  wattson.py                     gira in continuo (per systemd)
  wattson.py --dry-run           come sopra ma senza notifiche/sensori
  wattson.py fp                  elenca le impronte
  wattson.py name <fp> <nome>    dà un nome a un'impronta
  wattson.py learn white         misura il "bianco" (ultimi 2 min, deve essere stabile)
  wattson.py learn mark <dispositivo> <stato>   consumo attuale − bianco → profilo
                                 (e impronta con nome se ≥ 0,3 kW; stato "on" = nome semplice)
  wattson.py learn auto          profili dai cambi di stato osservati dopo il bianco
  wattson.py learn show          elenca i profili
  wattson.py catalog             rimette nel tuo catalogo (catalog-mine.json) le impronte con un Tipo
  wattson.py github              collega GitHub (una volta) per inviare al catalogo condiviso
  wattson.py dashboard [--force] crea la dashboard Wattson in HA con il logo (una volta, all'installazione)
                                 (+ vista "Impara" con i pulsanti bianco/impronta: la stessa taratura da HA)
Config: ~/.config/wattson/config.json (vedi config.example.json).
"""
import base64
import glob
import json
import math
import os
import queue
import re
import socket
import ssl
import struct
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

CONFIG_FILE = os.path.expanduser("~/.config/wattson/config.json")
DATA = os.path.expanduser("~/.local/share/wattson")
STOP = threading.Event()   # dentro HA: l'integrazione lo alza quando viene scaricata
PUBLISH = None             # dentro HA: il semaforo è un'entità vera, publish passa da qui
DEFAULTS = {
    "ha_url": "http://homeassistant.local:8123",
    "token_file": "~/.config/wattson/token",
    "power_entity": "sensor.main_power",
    # fotovoltaico (facoltativi): senza, il consumo della casa è il prelievo dal generale, come sempre
    "grid_signed": False,          # power_entity è lo scambio netto: negativo quando la casa immette in rete
    "export_entity": None,         # immissione in rete, se è un sensore a parte (positivo quando immette)
    "pv_entity": None,             # produzione dei pannelli
    "consumption_entity": None,    # consumo della casa già calcolato dall'inverter (vince sulla formula)
    # consiglio "usa il tuo sole" (si accende con grid_signed o export_entity): quando far partire i carichi pesanti
    "battery_level_entity": None,  # % di carica della batteria
    "battery_power_entity": None,  # potenza della batteria: positiva quando scarica (Sungrow)...
    "battery_charge_positive": False,   # ...o positiva quando carica
    "battery_full_pct": 95,        # sopra, la batteria è piena: l'immissione è davvero surplus
    "sun_load_kw": 2.0,            # immissione minima per consigliare un carico pesante
    "sun_min": 10,                 # minuti di condizione stabile prima di cambiare consiglio (nuvole)
    "feed_in_eur_kwh": None,       # quanto è pagata l'energia immessa (None = non so: niente risparmio in €)
    "limit_kw": 3.0,         # potenza impegnata
    "tolerance_kw": 3.3,     # oltre, il contatore può staccare
    "big_load_kw": 1.5,      # "carico pesante" per il semaforo
    "notify_entity": None,   # es. notify.mobile_app_telefono (None = niente notifiche)
    "price_eur_kwh": 0.25,   # tutto compreso, finché non c'è la bolletta
    "poll_s": 5,
    # notti da non usare per il carico di base: {entity_id: [stati "occupato"]}
    "base_skip": {},
    "submeters_exclude": [],   # sensori di potenza da ignorare
    # presa con misura che si sposta da un apparecchio all'altro per imparare le impronte:
    # il nome non è il suo ma quello scritto in "Wattson apparecchio" (input_text.wattson_apparecchio)
    "profiler": None,
    # dispositivi senza misura da osservare: {entity_id: "nome"}
    "watch": {},
    "dashboard": "wattson",   # url della dashboard creata da "wattson.py dashboard"
    # catalogo condiviso delle impronte (repo pubblico): si legge sempre, si scrive solo se catalog_share
    "catalog_repo": "tlabcustomaudio/wattson-catalog",
    "catalog_share": False,   # invia le impronte imparate col Profiler (con un Tipo); serve "wattson.py github"
    "github_client_id": "Iv23liRuAi6UXcBYWnXa",
    "language": None,         # lingua di notifiche e dashboard; None = quella di Home Assistant
    # bolletta (Italia, facoltativa): confronto con le offerte del Portale Offerte ARERA, solo materia energia
    # {"kwh_year": 2700, "eur_kwh": 0.12 o {"F1": .., "F2": .., "F3": ..} o "pun_spread": 0.02, "eur_year": 96,
    #  "resident": true, "region": "Lombardia", "comune": "015146"}  (regione e comune ISTAT: offerte locali)
    "bill": None,  # app GitHub "Wattson Catalog" (identificativo pubblico, niente segreti)
}

STEP_W = 150          # gradino minimo riconosciuto (a contatore calmo)
NOISE_WIN, NOISE_K = 40, 3   # soglia = max(STEP_W, K × rumore degli ultimi WIN campioni ≈ 10 min)
HOLD_N = 3            # campioni sopra metà soglia che confermano un gradino al limite (compressore da ~140 W)
MATCH_TOL = 0.25      # spegnimento accoppiato se |Δoff| entro ±25% di Δon
FP_TOL = 0.15         # stessa impronta se potenza entro ±15%
BASE_ALERT_W = 60     # avviso se il carico di base sale di almeno tanto
BASE_PLUG_W = 5       # una presa che di notte consuma almeno tanto più di prima è nominata nell'avviso
DIGEST_DAY, DIGEST_HOUR = 6, 19   # riepilogo settimanale: domenica alle 19
DIGEST_MIN_KWH = 0.5  # impronta senza nome che in una settimana consuma meno di così: non la chiedo
DIGEST_ASK = 3        # domande per riepilogo


# --- lingua: il testo è scritto in italiano, lang/<codice>.json lo traduce (inglese se manca la lingua) --------

LANG, TR, DASH_TR = "it", {}, {}


def T(s):
    return TR.get(s, s)


def set_lang(lang):
    """Lingua dell'installazione ("it", "en", "en-GB"…): traduzioni e testi fissi dei menu."""
    global LANG, TR, DASH_TR, NO_PLUG, NO_PROG, NO_FP, ON_MAIN, NO_WHAT, CAT_LABELS, STATES
    LANG, TR, DASH_TR = (lang or "it").split("-")[0].lower(), {}, {}
    if LANG != "it":
        d = os.path.join(os.path.dirname(os.path.realpath(__file__)), "lang")
        f = os.path.join(d, LANG + ".json")
        with open(f if os.path.exists(f) else os.path.join(d, "en.json")) as fh:
            tr = json.load(fh)
        TR, DASH_TR = tr["text"], tr["dashboard"]
    NO_PLUG, NO_PROG, NO_FP = T("nessuna presa con misura"), T("nessun ciclo ancora"), T("nessuna impronta ancora")
    ON_MAIN, NO_WHAT = T("sul generale"), T("niente da collocare")
    CAT_LABELS = {k: T(v) for k, v in (("type", "Tipo"), ("brand", "Marca"), ("model", "Modello"),
                                         ("code", "Codice prodotto"), ("profile", "Profilo"))}
    STATES = {"IDLE": T("a riposo"), "ON": T("acceso")}


set_lang("it")


def ha_lang(ha, cfg):
    """La lingua scelta in config, se no quella di Home Assistant (Impostazioni → Sistema → Generale)."""
    if cfg.get("language"):
        return cfg["language"]
    try:
        return ha.http("/api/config").get("language") or "it"
    except Exception as e:
        print("lingua di HA non letta:", e, flush=True)
        return "it"


# Tipo del catalogo: elenco fisso, chiave inglese nel catalogo condiviso, etichetta nella lingua dell'installazione
TYPES = {"Dishwasher": "Lavastoviglie", "Washing machine": "Lavatrice", "Tumble dryer": "Asciugatrice",
         "Washer dryer": "Lavasciuga", "Oven": "Forno", "Microwave": "Microonde", "Induction hob": "Piano a induzione",
         "Kettle": "Bollitore", "Coffee machine": "Macchina del caffè", "Toaster": "Tostapane", "Fridge": "Frigorifero",
         "Freezer": "Congelatore", "Dehumidifier": "Deumidificatore", "Air conditioner": "Condizionatore",
         "Heat pump": "Pompa di calore", "Electric heater": "Stufetta elettrica", "Water heater": "Scaldabagno",
         "Fan": "Ventilatore", "Air purifier": "Purificatore d'aria", "Vacuum cleaner": "Aspirapolvere",
         "Robot vacuum": "Robot aspirapolvere", "Hair dryer": "Phon", "Iron": "Ferro da stiro", "TV": "Televisore",
         "Computer": "Computer", "Network": "Rete (router, NAS)", "3D Printer": "Stampante 3D",
         "Printer": "Stampante", "Light": "Luce", "Pump": "Pompa", "Aquarium": "Acquario",
         "EV charger": "Wallbox (auto elettrica)", "Battery charger": "Caricabatterie", "Power tool": "Elettroutensile",
         "Other": "Altro"}
NO_TYPE = "—"


def type_label(key):
    if not key:
        return NO_TYPE
    return TYPES.get(key, key) if LANG == "it" else TR.get(TYPES.get(key, ""), key)


def type_options():
    return [NO_TYPE] + sorted((type_label(k) for k in TYPES), key=str.lower)


def type_key(v):
    """Dal menu (o da un valore vecchio scritto a mano) alla chiave del catalogo; "" se non è nell'elenco."""
    v = (v or "").strip().lower()
    return next((k for k in TYPES if v in (k.lower(), TYPES[k].lower(), type_label(k).lower())), "")


def set_field(ha, ent, value):
    """Scrive un campo del pannello: il Tipo è un menu (etichetta), gli altri testo."""
    if ent.startswith("input_select."):
        ha.http("/api/services/input_select/select_option", {"entity_id": ent, "option": type_label(value)})
    else:
        ha.http("/api/services/input_text/set_value", {"entity_id": ent, "value": value})


# --- logica pura (testata in test_wattson.py) --------------------------------

class Detector:
    """Da una serie (t, W) a eventi: step, cycle, transient."""

    def __init__(self, floor=None):
        self.floor = floor    # carico di base (W): i gradini accesi non possono superare level - floor
        self.level = None
        self.pend = None      # (t, w) primo campione fuori soglia, da confermare
        self.held = []        # campioni dopo pend rientrati solo in parte (sopra metà soglia, stesso verso)
        self.open = []        # gradini accesi: {"t", "w", "left" se già sceso di livello}
        self.win = []         # ultimi campioni, per il rumore

    def feed(self, t, w):
        ev = []
        self.win = (self.win + [w])[-NOISE_WIN:]
        if self.level is None:
            self.level = w
            return ev
        d = w - self.level
        # rumore = mediana dei salti tra campioni consecutivi (i gradini veri sono pochi e non la spostano);
        # alto (es. piatto di una stampante a impulsi) = soglia più alta, niente gradini finti
        noise = statistics.median(abs(a - b) for a, b in zip(self.win, self.win[1:])) if len(self.win) >= 10 else 0
        thr = max(STEP_W, NOISE_K * noise)
        if abs(d) < thr:
            if self.pend:
                pd = self.pend[1] - self.level
                if pd * d > 0 and abs(d) >= thr / 2:    # rientrato solo in parte: gradino vicino alla soglia
                    self.held.append(w)
                    if len(self.held) < HOLD_N:
                        return ev
                    new = statistics.median([self.pend[1]] + self.held)
                    ev += self._step(self.pend[0], new - self.level)
                    self.level, self.pend, self.held = new, None, []
                    return ev + self._fit(t)
                if pd > 0:    # un solo campione fuori soglia = carico breve
                    ev.append({"type": "transient", "t": self.pend[0], "w": round(pd)})
                self.pend, self.held = None, []
            self.level += 0.2 * d   # segue la deriva lenta (frigo, ecc.)
            return ev + self._fit(t)
        if self.pend and (self.pend[1] - self.level) * d > 0:
            new = (self.pend[1] + w) / 2
            ev += self._step(self.pend[0], new - self.level)
            self.level, self.pend, self.held = new, None, []
            ev += self._fit(t)
        else:
            self.pend, self.held = (t, w), []
        return ev

    def _fit(self, t):
        """Col fondo noto, i gradini accesi devono stare nello spazio sopra il fondo: un carico che cala piano
        (assorbito dalla deriva) o che si spegne dentro il rumore viene scalato, e chiuso se crolla."""
        if self.floor is None or not self.open:
            return []
        # mediana di ~10 min: gli impulsi (piatto di una stampante) non la spostano; level: un gradino sì
        room = max(0, max(self.level, statistics.median(self.win)) - self.floor)
        over = sum(o.get("left", o["w"]) for o in self.open) - room
        ev = []
        while over > STEP_W and self.open:
            o = max(self.open, key=lambda o: o["t"])    # l'ultimo acceso: spesso un impulso, non il carico lungo
            cut = min(over, o.get("left", o["w"]))
            o["left"] = o.get("left", o["w"]) - cut
            over -= cut
            # ponytail: "sceso sotto 1/4 del picco = spento" è euristico; un carico che a regime sta
            # sotto 1/4 del suo spunto si chiude presto. Serve il sotto-contatore o un profilo per stato.
            if o["left"] < max(STEP_W, 0.25 * o["w"]):
                self.open.remove(o)
                ev.append({"type": "cycle", "t": o["t"], "end": t, "w": round(o["w"]),
                           "min": round((t - o["t"]) / 60, 1)})
        return ev

    def _step(self, t, dw):
        if dw > 0:
            self.open.append({"t": t, "w": dw})
            return [{"type": "on", "t": t, "w": round(dw)}]
        best = None
        for o in self.open:
            r = -dw / o.get("left", o["w"])
            if 1 - MATCH_TOL <= r <= 1 + MATCH_TOL and (best is None or abs(r - 1) < abs(-dw / best.get("left", best["w"]) - 1)):
                best = o
        if not best:
            # discesa senza gradino corrispondente (carico a più livelli, es. riscaldamento → mantenimento):
            # i gradini accesi li scala _fit contro il fondo
            return [{"type": "off", "t": t, "w": round(dw)}]
        self.open.remove(best)
        return [{"type": "cycle", "t": best["t"], "end": t, "w": round(best["w"]),
                 "min": round((t - best["t"]) / 60, 1)}]


def match_fp(fps, w, minutes, clues=None, areas=None):
    """Impronta di un gradino di w W durato minutes min. Se ne vanno bene più d'una (tre deumidificatori da ~200 W),
    vince quella i cui indizi di stanza somigliano di più a quelli di adesso, poi la più vicina in potenza."""
    ok = [fp for fp in fps if abs(w - fp["w"]) <= FP_TOL * fp["w"]
          and (fp["min"] is None or fp["min"] / 2.5 <= minutes <= fp["min"] * 2.5)]
    return max(ok, key=lambda fp: (clue_score(fp, clues or [], (areas or {}).get(fp["id"])), -abs(w - fp["w"])),
               default=None)


# --- indizi dalle stanze: cosa succede nella stanza quando un carico parte o gira -------------------------
CLUE_DOMAINS = ("switch", "light", "climate", "fan", "humidifier", "media_player", "cover", "water_heater", "vacuum")
CLUE_BINARY = ("motion", "occupancy", "presence", "door", "window")
TREND = {"humidity": ("umidità", 1.0), "temperature": ("temperatura", 0.8)}   # soglia: %RH, °C
ROOM_MIN_RUNS, ROOM_MIN_SHARE = 2, 0.6


def room_clues(states, ent_area, t, before=60, after=20):
    """Entità con una stanza che hanno cambiato stato attorno a t (luce accesa, clima avviato, porta aperta,
    movimento, stampante che parte): ["Stanza|Nome: stato"]."""
    out = set()
    for s in states:
        e, a = s["entity_id"], s.get("attributes", {})
        dom, area = e.split(".")[0], ent_area.get(e)
        if not area or s["state"] in ("unavailable", "unknown") or not (
                dom in CLUE_DOMAINS or (dom == "binary_sensor" and a.get("device_class") in CLUE_BINARY)
                or (dom == "sensor" and a.get("device_class") == "enum")):
            continue
        if t - before <= datetime.fromisoformat(s["last_changed"]).timestamp() <= t + after:
            out.add("%s|%s: %s" % (area, a.get("friendly_name", e), s["state"]))
    return sorted(out)


def env_sensors(states, ent_area):
    """Sensori di umidità e temperatura di ogni stanza: {stanza: {"humidity": [...], "temperature": [...]}}."""
    out = {}
    for s in states:
        dc, area = s.get("attributes", {}).get("device_class"), ent_area.get(s["entity_id"])
        if area and dc in TREND and s["entity_id"].startswith("sensor."):
            out.setdefault(area, {}).setdefault(dc, []).append(s["entity_id"])
    return out


def trend_clues(series, env, t0, t1):
    """Umidità o temperatura di una stanza cambiate mentre il carico era acceso (t0→t1), al netto di come
    andavano nei 20 minuti prima: deumidificatore = umidità giù, forno o stufa = temperatura su, clima = giù."""
    def at(pts, t):
        v = None
        for tt, x in pts:
            if tt > t:
                break
            v = x
        return v
    out = set()
    for area, kinds in env.items():
        for kind, ents in kinds.items():
            word, thr = TREND[kind]
            for e in ents:
                pts = series.get(e) or []
                if kind == "temperature" and any(v > 45 for _, v in pts):
                    continue      # ugello, piatto, forno: temperatura di un apparecchio, non della stanza
                pre, v0, v1 = at(pts, t0 - 1200), at(pts, t0), at(pts, t1)
                if None not in (pre, v0, v1) and abs((v1 - v0) - (v0 - pre)) >= thr:
                    out.add("%s|%s %s" % (area, word, "↓" if (v1 - v0) - (v0 - pre) < 0 else "↑"))
    return sorted(out)


def clue_learn(fp, clues):
    """Conta gli indizi dei cicli di un'impronta; clue_runs = cicli in cui le stanze erano osservate."""
    fp["clue_runs"] = fp.get("clue_runs", 0) + 1
    c = fp.setdefault("clues", {})
    for k in clues:
        c[k] = c.get(k, 0) + 1
    if len(c) > 30:   # ponytail: tiene i 30 più frequenti; se non bastano, una tabella a parte
        fp["clues"] = dict(sorted(c.items(), key=lambda x: -x[1])[:30])


def clue_score(fp, clues, area=None):
    """Quanto gli indizi di adesso somigliano a quelli già visti per l'impronta (quota dei suoi cicli in cui
    c'erano); la stanza detta dall'utente, se ha un indizio adesso, vale come un indizio sicuro."""
    runs = fp.get("clue_runs") or 0
    s = sum(fp.get("clues", {}).get(k, 0) / runs for k in clues) if runs else 0
    return s + (1 if area and any(k.split("|")[0] == area for k in clues) else 0)


def room_guess(fp):
    """Stanza probabile dagli indizi: quella che si ripete in almeno 2 cicli e nel 60% di quelli osservati."""
    runs, per = fp.get("clue_runs") or 0, {}
    for k, n in fp.get("clues", {}).items():
        per[k.split("|")[0]] = max(per.get(k.split("|")[0], 0), n)   # più indizi dello stesso ciclo non si sommano
    per.pop(fp.get("guess_no"), None)      # l'utente ha detto che non è lì
    best = max(per.items(), key=lambda x: x[1], default=None)
    return best[0] if best and best[1] >= ROOM_MIN_RUNS and best[1] >= ROOM_MIN_SHARE * runs else None


# --- Telegram a due vie: domande con pulsanti, oppure risposta scritta (bot di HA, integrazione telegram_bot) ---
def ask_add(st, kind, fp_id, text, extra=None):
    """Registra una domanda aperta; ritorna il suo id, corto perché sta nei 64 byte di un pulsante di Telegram.
    Una risposta scritta vale per l'ultima domanda aperta (ask_last)."""
    n = st.get("ask_n", 0) + 1
    qid, asks = "a%d" % n, st.setdefault("asks", {})
    asks[qid] = dict(extra or {}, kind=kind, fp=fp_id, text=text)
    for k in sorted(asks, key=lambda k: int(k[1:]))[:-10]:     # restano aperte le ultime 10
        del asks[k]
    st["ask_n"], st["ask_last"] = n, qid
    return qid


def ask_close(st, qid):
    st.get("asks", {}).pop(qid, None)
    if st.get("ask_last") == qid:
        st["ask_last"] = max(st.get("asks", {}), key=lambda k: int(k[1:]), default=None)


def btn(label, data):
    """Un pulsante (una riga): HA divide le righe su "," e testo/comando su ":"."""
    return "%s:%s" % (label.replace(",", " ").replace(":", " ")[:40], data)


def room_question(st, fp, guess):
    """Stanza indovinata dagli indizi: (testo, pulsanti)."""
    why = ", ".join(k.split("|")[1] for k in fp.get("clues", {}) if k.startswith(guess + "|"))
    text = (T("📍 %s (+%s kW) sembra stare in %s (%s).\nÈ giusto? Tocca un pulsante, oppure scrivimi la stanza giusta.")
            % (fp["name"] or fp["id"], it(fp["w"] / 1000, 1), guess, why))
    qid = ask_add(st, "room", fp["id"], text, {"area": guess})
    return text, [btn("✅ Sì, " + guess, "/w %s y" % qid), btn("❌ No", "/w %s n" % qid)]


def name_question(st, fp, like):
    """Impronta senza nome dal riepilogo settimanale: (testo, pulsanti con i suggerimenti del catalogo)."""
    text = (T("🔎 Impronta nuova (%s): +%s kW per circa %d min, vista %s (%s).\nChe apparecchio è? Scrivimi il "
            "nome%s.") % (fp["id"], it(fp["w"] / 1000, 1), round(fp["min"] or 0),
                         T("1 volta") if fp["n"] == 1 else T("%d volte") % fp["n"],
                         ", ".join(fmt_t(x) for x in fp.get("seen", [])[-3:]),
                         T(", o tocca un suggerimento del catalogo condiviso") if like else ""))
    qid = ask_add(st, "name", fp["id"], text, {"like": like[:3]})
    return text, [btn(l, "/w %s c%d" % (qid, i)) for i, l in enumerate(like[:3])] + [btn(T("🤷 Non so"), "/w %s n" % qid)]


def match_area(text, area_names):
    """La stanza scritta a mano: uguale, o l'unica che la contiene ("lavand" → Lavanderia)."""
    t = text.strip().lower()
    same = [a for a in area_names if a.lower() == t]
    part = [a for a in area_names if t and t in a.lower()]
    return (same or (part if len(part) == 1 else [None]))[0]


def tg_handle(st, fps, where, area_names, ev):
    """Evento del bot → risposta da mandare (None = non è per Wattson). Pulsante: data "/w <domanda> <scelta>";
    testo scritto: risponde all'ultima domanda aperta. Aggiorna impronte e "Dove sta?"."""
    d = ev.get("data") or {}
    if ev.get("event_type") == "telegram_callback":
        parts = (d.get("data") or "").split()
        if len(parts) != 3 or parts[0] != "/w":
            return None
        qid, choice, free = parts[1], parts[2], None
    else:
        free = (d.get("text") or "").strip()
        qid, choice = st.get("ask_last"), None
        if not free or free.startswith("/"):
            return None          # comandi di altre automazioni
        if not qid:
            return T("📨 Ricevuto, ma adesso non ho domande aperte.")
    a = st.get("asks", {}).get(qid)
    if not a:
        return T("Questa domanda è già chiusa.")
    if a["kind"] == "line":
        if choice not in ("y", "n"):
            return T("Rispondi con un pulsante, oppure scegli la linea in Impara → 📍 Dove sta.")
        ask_close(st, qid)
        if choice == "n":
            st.setdefault("line_votes", {}).setdefault(a["plug"], {})[a["line"]] = -1
            return T("Ok, %s non è dentro %s. Non te lo chiedo più.") % (a["plug"], a["line"])
        where["plug:" + a["plug"]] = {"area": where.get("plug:" + a["plug"], {}).get("area"), "line": a["line"]}
        return T("✅ %s: dentro %s.") % (a["plug"], a["line"])
    fp = next((f for f in fps if f["id"] == a["fp"]), None)
    if fp is None:
        ask_close(st, qid)
        return T("❌ Quell'impronta non c'è più.")
    if a["kind"] == "room":
        if choice == "n":
            fp["guess_no"] = a["area"]
            a["area"] = None     # resta aperta: la stanza giusta si può ancora scrivere
            return T("Ok, non è in %s. Scrivimi la stanza giusta, o sceglila in Impara → 📍 Dove sta.") % fp.get("guess_no")
        area = a["area"] if choice == "y" else match_area(free, area_names)
        if not area:
            return T("Non trovo la stanza «%s». Stanze: %s.") % (free, ", ".join(area_names))
        where["fp:" + fp["id"]] = {"area": area, "line": where.get("fp:" + fp["id"], {}).get("line")}
        ask_close(st, qid)
        return T("✅ %s: stanza %s.") % (fp["name"] or fp["id"], area)
    if choice == "n":
        ask_close(st, qid)
        return T("Ok, resta senza nome. Se ti viene in mente, scrivimelo o daglielo in Impara.")
    name = a["like"][int(choice[1:])] if choice else free
    fp["name"] = name
    ask_close(st, qid)
    return T("✅ %s ora si chiama «%s».") % (fp["id"], name)


def tg_listen(cfg, token, q):
    """Thread: eventi del bot di HA (pulsanti e messaggi) nella coda q. HA li genera solo per le chat
    autorizzate nell'integrazione telegram_bot: nessun altro può rispondere a Wattson."""
    while not STOP.is_set():
        try:
            ws = WS(cfg["ha_url"], token)
            for ev in ("telegram_callback", "telegram_text"):
                ws.call(type="subscribe_events", event_type=ev)
            ws.s.settimeout(300)       # HA manda un ping ogni minuto circa: 5 min di silenzio = connessione morta
            while True:
                r = ws.recv()
                if r.get("type") == "event":
                    q.put(r["event"])
        except Exception as e:
            print("telegram: riconnetto (%s)" % e, flush=True)
            STOP.wait(30)


def seed_fp(fps, name, w):
    """Impronta creata a mano (taratura): nome e potenza noti, durata da imparare."""
    for fp in fps:
        if fp["name"] == name:
            fp["w"], fp["manual"] = round(w), True
            return fp
    fp = {"id": "fp%d" % (len(fps) + 1), "name": name, "w": round(w), "min": None,
          "n": 1, "seen": [], "manual": True}
    fps.append(fp)
    return fp


def learn(fps, cyc, name=None, clues=None, areas=None, plugs=()):
    """Aggiorna le impronte con un ciclo; ritorna l'impronta toccata. Con name (ciclo misurato da una presa)
    cerca solo tra le impronte con quel nome o senza nome. Senza name non può essere un apparecchio con la sua
    presa (plugs: i loro nomi), che sarebbe salita: forno da 2 kW scambiato per la lavastoviglie da 2 kW.
    clues: indizi delle stanze (None = non osservate)."""
    pool = [f for f in fps if not any(of_plug(f, p) for p in plugs)] if name is None else [f for f in fps if f["name"] in (name, None)]
    fp = match_fp(pool, cyc["w"], cyc["min"], clues, areas)
    wh = round(cyc["w"] * (cyc["min"] or 0) / 60)
    if fp:
        n, wh = fp["n"], fp_wh(fp) + wh
        fp["w"] = round((fp["w"] * n + cyc["w"]) / (n + 1))
        fp["min"] = cyc["min"] if fp["min"] is None else round((fp["min"] * n + cyc["min"]) / (n + 1), 1)
        fp["n"] = n + 1
        fp["seen"] = (fp.get("seen", []) + [cyc["t"]])[-10:]
    else:
        fp = {"id": "fp%d" % (len(fps) + 1), "name": None, "w": cyc["w"], "min": cyc["min"],
              "n": 1, "seen": [cyc["t"]]}
        fps.append(fp)
    fp["wh"] = wh
    if name and fp["name"] is None:
        fp["name"] = name
    if clues is not None:
        clue_learn(fp, clues)
    return fp


def fp_wh(fp):
    """Energia di un'impronta finora; le impronte di prima che si contasse: potenza × durata media × volte."""
    return fp.get("wh", round(fp["w"] * (fp["min"] or 0) / 60 * fp["n"]))


def label(fp, w):
    if fp and fp["name"]:
        return fp["name"]
    return T("carico da %s kW") % it(w / 1000, 1)


def flows(cfg, v):
    """Dalle letture ai flussi della casa. "home" (consumo) serve a riconoscere gli apparecchi: col fotovoltaico
    il prelievo dalla rete sparisce quando il sole copre il carico, il consumo no. "net" (prelievo − immissione)
    serve al semaforo: il contatore stacca solo sul prelievo, e l'immissione è margine in più.
    Senza fotovoltaico configurato: home = net = import = il generale, come prima."""
    g = v["grid"]
    imp, exp = (max(g, 0), max(-g, 0)) if cfg.get("grid_signed") else (g, v.get("export") or 0)
    if v.get("home") is not None:
        home = v["home"]
    else:   # ponytail: batteria non ancora gestita (serve lo schema dei sensori di una casa con batteria)
        home = imp - exp + (v.get("pv") or 0)
    bat = v.get("bat")
    if bat is not None and cfg.get("battery_charge_positive"):
        bat = -bat
    return {"home": max(home, 0), "import": imp, "export": exp, "net": imp - exp, "pv": v.get("pv"),
            "soc": v.get("soc"), "bat": bat}


SUN_DAY_MAX = 4     # aperture di surplus notificate al giorno, come un semaforo che non insiste


def sun_now(fl, cfg):
    """Che energia useresti adesso per un carico pesante: "sun" surplus (immissione con batteria piena), "battery"
    (la batteria si sta caricando: un carico ora le toglie la scorta della sera), "grid" (batteria o rete)."""
    full = fl["soc"] is None or fl["soc"] >= cfg["battery_full_pct"]
    if fl["export"] >= cfg["sun_load_kw"] * 1000 and full:
        return "sun"
    if fl["bat"] is not None and fl["bat"] < -200 and not full:
        return "battery"
    return "grid"


def sun_step(s, cand, now, wait_s):
    """Cambia consiglio solo se il nuovo vale da wait_s secondi di fila: una nuvola non apre e chiude la finestra.
    s = {"state", "since"}; ritorna True se è cambiato."""
    if s.get("state") is None or cand == s["state"]:
        s.update(state=s.get("state") or cand, since=None)
        return False
    s["since"] = s.get("since") or now
    if now - s["since"] < wait_s:
        return False
    s.update(state=cand, since=None)
    return True


def sun_fits(exp_w, loads):
    """Carichi pesanti noti {nome: W} che stanno nell'immissione, dal più grande."""
    out, left = [], exp_w
    for n, w in sorted(loads.items(), key=lambda x: -x[1]):
        if w <= left:
            out.append("%s %s kW" % (n, it(w / 1000, 1)))
            left -= w
    return out


def sun_advice(state, fl, cfg, loads):
    if state == "sun":
        fits = sun_fits(fl["export"], loads)
        save = cfg["feed_in_eur_kwh"]
        return T("☀️ Usa il tuo sole: immetti %s kW%s.%s%s") % (
            it(fl["export"] / 1000, 1), "" if fl["soc"] is None else T(" e la batteria è piena (%d %%)") % fl["soc"],
            T(" Ci stanno: %s.") % " + ".join(fits) if fits else T(" È il momento dei carichi pesanti."),
            "" if save is None else T(" Ogni kWh usato ora invece che comprato risparmia %s €.") % it(
                cfg["price_eur_kwh"] - save))
    if state == "battery":
        return (T("⏳ La batteria si sta caricando (%d %%): un carico pesante adesso le toglie l'energia della sera. "
                "Se può aspettare, aspetta che sia piena.") % (fl["soc"] or 0))
    return T("🌙 Adesso un carico pesante va a batteria o rete: se può aspettare, rimandalo al prossimo surplus di sole.")


def light(p_w, cfg, prev, open_steps, fps, now):
    """Semaforo: (stato, consiglio)."""
    limit, tol, big = cfg["limit_kw"] * 1000, cfg["tolerance_kw"] * 1000, cfg["big_load_kw"] * 1000
    if p_w >= limit or (prev == "stop" and p_w > limit - 300):
        return "stop", T("Spegni qualcosa: %s kW, oltre %s kW il contatore può staccare.") % (it(p_w / 1000), it(tol / 1000))
    margin = tol - p_w
    if margin < big:
        eta = None
        for o in open_steps:
            fp = match_fp(fps, o["w"], 0.01) or next((f for f in fps if abs(o["w"] - f["w"]) <= FP_TOL * f["w"]), None)
            if fp and fp["n"] >= 2 and fp["min"]:
                left = fp["min"] - (now - o["t"]) / 60
                if left > 0:
                    eta = left if eta is None else min(eta, left)
        tail = T(" Tra circa %d min dovrebbe liberarsi.") % max(1, round(eta)) if eta else ""
        return "wait", T("Margine %s kW: aspetta per forno, bollitore o phon.%s") % (it(margin / 1000), tail)
    return "go", T("Via libera: margine %s kW.") % it(margin / 1000, 1)


def discover(states, main, exclude):
    """Sensori di potenza (tranne il generale): {entity_id: (nome, fattore→W)}."""
    out = {}
    for s in states:
        a, e = s.get("attributes", {}), s["entity_id"]
        unit = a.get("unit_of_measurement")
        if (a.get("device_class") == "power" and unit in ("W", "kW") and e != main
                and e not in exclude and not e.startswith("sensor.wattson")
                and "calculation_mode" not in a):      # powercalc & co.: potenza stimata, non misurata

            name = a.get("friendly_name", e)
            for suf in (" power", " Power", " potenza", " Potenza", " 0"):
                name = name[:-len(suf)] if name.endswith(suf) else name
            out[e] = (name, 1000 if unit == "kW" else 1)
    return out


PROFILER_NAME = "profiler"


def find_profiler(subs):
    """La presa Profiler si trova dal nome: basta chiamarla "Profiler" in HA."""
    return next((e for e, (n, _) in sorted(subs.items()) if n.strip().lower() == PROFILER_NAME), None)


def fp_name_from(sub, profiler, typed):
    """Nome per un'impronta misurata dal sotto-contatore sub. La presa "profiler"
    non dà il suo nome ma quello dell'apparecchio attaccato (typed; vuoto = nessuno)."""
    if sub is not None and sub == profiler:
        typed = (typed or "").strip()
        return None if typed in ("", "unknown", "unavailable") else typed
    return sub


def who_stepped(hist, t, dw):
    """Sotto-contatore che è salito di ~dw attorno a t. hist: {nome: [(t, W)]}."""
    for name, h in hist.items():
        before = [w for ts, w in h if t - 60 <= ts < t - 5]
        after = [w for ts, w in h if ts >= t]
        if before and after and abs((max(after) - min(before)) - dw) <= MATCH_TOL * dw:
            return name
    return None


LINE_MIN_W = 100     # gradino di una presa abbastanza grande da leggerlo anche sulla sua linea
LINE_VOTES = 3       # volte che presa e linea salgono insieme prima di chiedere


def plug_rise(h, now):
    """W saliti di colpo da un misuratore (ultimo campione contro la mediana di 15-60 s prima), o None."""
    before = [w for ts, w in h if now - 60 <= ts < now - 15]
    if not h or len(before) < 3:
        return None
    dw = h[-1][1] - statistics.median(before)
    return dw if dw >= LINE_MIN_W else None


def line_vote(votes, hist, plug, t0, dw):
    """La presa plug è salita di dw a t0: un altro misuratore salito di almeno ~dw (e che misura almeno quanto lei)
    può essere la sua linea a monte, +1; uno rimasto fermo non può esserlo, mai più (-1). Ritorna le linee con
    abbastanza voti. votes {presa: {linea: n}}."""
    v = votes.setdefault(plug, {})
    mine = [w for ts, w in hist.get(plug, []) if ts >= t0 + 3]
    for line, h in hist.items():
        before = [w for ts, w in h if t0 - 60 <= ts < t0 - 3]
        after = [w for ts, w in h if ts >= t0 + 3]
        if line == plug or not before or not after or not mine or v.get(line) == -1:
            continue
        d = statistics.median(after) - statistics.median(before)
        if d >= (1 - MATCH_TOL) * dw and statistics.median(after) >= 0.95 * statistics.median(mine):
            v[line] = v.get(line, 0) + 1
        elif d < 0.5 * dw:
            v[line] = -1
    return [line for line, n in v.items() if n >= LINE_VOTES]


def line_question(st, plug, line):
    text = (T("🔌 La presa %s sembra stare sulla linea %s: sono salite insieme %d volte.\nÈ giusto? (Così i suoi watt non "
            "si contano due volte.)") % (plug, line, LINE_VOTES))
    qid = ask_add(st, "line", None, text, {"plug": plug, "line": line})
    return text, [btn(T("✅ Sì, dentro ") + line, "/w %s y" % qid), btn("❌ No", "/w %s n" % qid)]


def state_hint(states, watch, t, window=60):
    """Dispositivo osservato che ha cambiato stato attorno a t (states: entity -> (stato, ts))."""
    best = None
    for e, name in watch.items():
        st, ts = states.get(e, (None, None))
        if ts is not None and t - window <= ts <= t + 20 and st not in ("unavailable", "unknown"):
            if best is None or abs(ts - t) < best[0]:
                best = (abs(ts - t), "%s: %s" % (name, st))
    return best[1] if best else None


def hint_name(fp, hint, need=2):
    """Conta gli indizi di un'impronta; ritorna il nome quando uno si ripete."""
    if not hint:
        return None
    h = fp.setdefault("hints", {})
    h[hint] = h.get(hint, 0) + 1
    return hint if h[hint] >= need else None


def steady(samples, t_end, seconds, max_spread=40):
    """Mediana della potenza in [t_end-seconds, t_end] se stabile, altrimenti None."""
    w = sorted(v for t, v in samples if t_end - seconds <= t <= t_end)
    if len(w) < 4:
        return None
    if w[int(len(w) * 0.9) - 1] - w[len(w) // 10] > max_spread:
        return None
    return statistics.median(w)


def segments(changes, t_from, t_to):
    """Dai cambi di stato [(t, dispositivo, stato)] ai tratti (dispositivo, stato, t0, t1)."""
    out, cur = [], {}
    for t, dev, st in sorted(changes):
        if dev in cur:
            out.append((dev, cur[dev][1], cur[dev][0], t))
        cur[dev] = (t, st)
    out += [(dev, st, t0, t_to) for dev, (t0, st) in cur.items()]
    return [x for x in out if x[3] > t_from and x[1] not in ("unavailable", "unknown")]


def busy(states, rules):
    """Entità (da rules) che sono in uno stato "occupato"."""
    return [e for e, bad in rules.items() if states.get(e) in bad]


def it(v, d=2):
    """Numero col separatore decimale della lingua."""
    return ("%%.%df" % d % v).replace(".", "," if LANG == "it" else ".")


def eur_year(watts, price):
    return watts * 24 * 365 / 1000 * price


# --- I/O --------------------------------------------------------------------

def load_cfg():
    cfg = dict(DEFAULTS)
    with open(CONFIG_FILE) as f:
        cfg.update(json.load(f))
    return cfg


def jload(name, default):
    try:
        with open(os.path.join(DATA, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(name, obj):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def log_event(ev):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "events.jsonl"), "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


class HA:
    def __init__(self, cfg, dry):
        self.cfg, self.dry = cfg, dry
        self.token = cfg.get("token") or open(os.path.expanduser(cfg["token_file"])).read().strip()

    def http(self, path, payload=None):
        req = urllib.request.Request(self.cfg["ha_url"] + path)
        req.add_header("Authorization", "Bearer " + self.token)
        data = None
        if payload is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(payload).encode()
        with urllib.request.urlopen(req, data=data, timeout=20) as r:
            return json.loads(r.read())

    def read_w(self, e):
        s = self.http("/api/states/" + e)
        v = float(s["state"])   # ValueError su unavailable
        unit = s.get("attributes", {}).get("unit_of_measurement", "W")
        return v * 1000 if unit == "kW" else v, s["last_updated"]

    def power_w(self):
        """Letture dei sensori di rete e fotovoltaico ({grid, export, pv, home} in W) e istante dell'ultimo
        aggiornamento. Il generale deve esserci (errore = riprova); gli altri possono mancare (inverter che di
        notte dorme = niente produzione)."""
        v, upd = {}, []
        v["grid"], u = self.read_w(self.cfg["power_entity"])
        upd.append(u)
        for k, ek in (("export", "export_entity"), ("pv", "pv_entity"), ("home", "consumption_entity"),
                      ("soc", "battery_level_entity"), ("bat", "battery_power_entity")):
            e = self.cfg[ek]
            if e:
                try:
                    v[k], u = self.read_w(e)
                    upd.append(u)
                except (ValueError, KeyError, OSError):
                    v[k] = None
        return v, max(upd)

    def notify(self, msg):
        print("NOTIFY:", msg.replace("\n", " | "), flush=True)
        if not self.dry and self.cfg["notify_entity"]:
            self.http("/api/services/notify/send_message",
                      {"message": msg, "title": "Wattson", "entity_id": self.cfg["notify_entity"]})

    def ask(self, text, buttons):
        """Domanda su Telegram con pulsanti, tramite il bot di HA."""
        print("ASK:", text.replace("\n", " | "), buttons, flush=True)
        if not self.dry:
            self.http("/api/services/telegram_bot/send_message", {"message": text, "inline_keyboard": buttons})

    def tg_reply(self, ev, text, question=None):
        """Esito di una risposta: conferma il pulsante (se no su Telegram la rotellina gira) e scrive l'esito sotto
        la domanda, togliendo i pulsanti; una risposta scritta riceve un messaggio nuovo."""
        d = ev.get("data") or {}
        print("TG:", text, flush=True)
        if self.dry:
            return
        if ev.get("event_type") == "telegram_callback":
            self.http("/api/services/telegram_bot/answer_callback_query",
                      {"callback_query_id": d.get("id"), "message": text[:190]})
            mid = (d.get("message") or {}).get("message_id")
            if mid and question:
                self.http("/api/services/telegram_bot/edit_message",
                          {"message_id": mid, "chat_id": d.get("chat_id"), "message": question + "\n\n→ " + text})
                return
        reply = {"reply_to_message_id": d["id"]} if isinstance(d.get("id"), int) else {}   # sotto il tuo messaggio
        self.http("/api/services/telegram_bot/send_message", dict(reply, message=text))

    def publish(self, state, attrs):
        if PUBLISH:
            PUBLISH(state, attrs)
        elif not self.dry:
            self.http("/api/states/sensor.wattson_semaforo", {"state": state, "attributes": attrs})


watch_cache = {}


def watched(ha, cfg, p=None):
    """Stato e istante dell'ultimo cambio dei dispositivi osservati; registra i cambi."""
    for e in cfg["watch"]:
        try:
            s = ha.http("/api/states/" + e)
        except Exception:
            continue
        new = (s["state"], datetime.fromisoformat(s["last_changed"]).timestamp())
        old = watch_cache.get(e)
        if old and old != new:
            log_event({"type": "state", "t": new[1], "dev": cfg["watch"][e], "from": old[0],
                       "to": new[0], "p_now": None if p is None else round(p)})
        watch_cache[e] = new
    return watch_cache


def log_sample(t, w):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "samples-%s.csv" % datetime.fromtimestamp(t).date()), "a") as f:
        f.write("%d,%d\n" % (t, w))


def read_samples(t_from):
    out, d = [], datetime.fromtimestamp(t_from).date()
    while d <= datetime.now().date():
        day, d = d, d + timedelta(days=1)
        try:
            with open(os.path.join(DATA, "samples-%s.csv" % day)) as f:
                out += [(int(a), int(b)) for a, b in (l.strip().split(",") for l in f) if int(a) >= t_from]
        except OSError:
            pass
    return out


def fmt_t(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def run(dry, cfg=None):
    cfg = cfg or load_cfg()
    ha = HA(cfg, dry)
    set_lang(ha_lang(ha, cfg))
    fps = jload("fingerprints.json", [])
    st = jload("state.json", {"light": "go", "night": [], "night_day": None, "bases": []})
    det = Detector()
    transients = []
    last_upd = None
    subs, subs_at, hist, on_names, on_hints = {}, 0, {}, {}, {}
    ui_on, prof_on, ui_seen, job, ui_msg, ui_opts = False, False, {}, None, None, None
    plug_on, plug_opts, plugs, progs = False, None, jload("plugs.json", {}), jload("programs.json", {})
    prog_opts, runs_saved = None, 0
    where, ctx, where_on, wopts = jload("where.json", {}), ([], {}, {}, {}), False, {}
    env, on_clues = {}, {}      # sensori di umidità/temperatura per stanza; indizi di ogni gradino acceso
    tgq, tg_on = queue.Queue(), False     # Telegram a due vie: risposte dal bot di HA
    if not dry:
        try:
            tg_on = any(d["domain"] == "telegram_bot" for d in ha.http("/api/services"))
        except Exception as e:
            print("servizi di HA non letti:", e, flush=True)
        if tg_on:
            threading.Thread(target=tg_listen, args=(cfg, ha.token, tgq), daemon=True).start()
    auto_prof = not cfg["profiler"]      # in config vince; se no, la presa che si chiama "Profiler"
    print("Wattson v0 avviato su", cfg["power_entity"], flush=True)
    while not STOP.is_set():
        try:
            fl, upd = ha.power_w()
            fl = flows(cfg, fl)
            p = fl["home"]      # consumo della casa: riconoscimento, carico di base, profili
        except Exception as e:   # HA giù o sensore unavailable: riprova
            print("lettura fallita:", e, flush=True)
            STOP.wait(30)
            continue
        now = time.time()
        if now - subs_at > 3600:        # nuovi dispositivi entrano da soli
            try:
                states = ha.http("/api/states")
                subs = discover(states, cfg["power_entity"], cfg["submeters_exclude"])
                if auto_prof:
                    cfg["profiler"] = find_profiler(subs)
                ids = {s_["entity_id"] for s_ in states}
                ui_on = set(UI.values()) <= ids          # pannello "Impara" presente
                prof_on = set(UI_PROF.values()) <= ids   # pulsanti della presa profiler (installazioni vecchie: no)
                plug_on = set(UI_PLUG.values()) <= ids   # dati del catalogo per le prese fisse
                where_on = set(UI_WHERE.values()) <= ids   # pannello "Dove sta?"
                tipo = next((s_ for s_ in states if s_["entity_id"] == UI_PROF["tipo"]), None)
                if tipo and tipo["attributes"].get("options") != type_options():
                    ha.http("/api/services/input_select/set_options",
                            {"entity_id": UI_PROF["tipo"], "options": type_options()})
                subs_at = now
                print("sotto-contatori:", ", ".join(n for n, _ in subs.values()) or "nessuno", flush=True)
            except Exception as e:
                print("discovery fallita:", e, flush=True)
            try:
                ctx = ha_context(WS(cfg["ha_url"], ha.token), subs)   # stanze e linee a monte, da HA
                env = env_sensors(ha.http("/api/states"), ctx[3])
            except Exception as e:
                print("stanze di HA non lette:", e, flush=True)
            try:
                out = backfill_plugs(ha, cfg, fps, st, subs, now, progs)
                if out:
                    ha.notify(out)
                if now - st.get("shared_at", 0) > SHARE_EVERY:
                    out = share(fps, plugs, progs, cfg, now, plug_levels(st))
                    st["shared_at"] = now
                    jsave("state.json", st)
                    if out:
                        ha.notify(out)
            except Exception as e:
                print("prese fisse:", e, flush=True)
            if cfg["bill"] and now - st.get("offers_at", 0) > 30 * 86400:     # offerte: una volta al mese
                try:
                    r, cons = offers_check(cfg, st["bases"][-1]["w"] if st["bases"] else None)
                    if r["offers"] and r["offers"][0]["save"] >= OFFER_SAVE_MIN:
                        ha.notify(savings_msg(r, cons))
                    st["offers_at"] = now
                    jsave("state.json", st)
                except Exception as e:
                    print("offerte ARERA non lette:", e, flush=True)
            try:
                rp = os.path.join(DATA, "catalog-remote.json")
                # oltre alle 24h, si riscarica anche se un nostro invio precedente potrebbe non essere ancora
                # arrivato nella copia locale (mtime del file più vecchio di quell'invio): la CDN di GitHub
                # a volte serve per qualche minuto una versione appena superata, un ciclo dopo è passata
                stale = st.get("catalog_sent_at", 0) > (os.path.getmtime(rp) if os.path.exists(rp) else 0)
                if not os.path.exists(rp) or now - os.path.getmtime(rp) > 86400 or stale:
                    fetch_catalog(cfg)
                out = send_outbox(cfg)
                if out and out.startswith("📤"):
                    st["catalog_sent_at"] = now
                    jsave("state.json", st)
                if out and (not out.startswith("🔑") or now - st.get("gh_warned", 0) > 86400):
                    ha.notify(out)     # "ricollega GitHub": al massimo una volta al giorno
                    if out.startswith("🔑"):
                        st["gh_warned"] = now
                        jsave("state.json", st)
            except Exception as e:
                print("catalogo condiviso non raggiungibile:", e, flush=True)
        if cfg["watch"] and int(now) % 30 < cfg["poll_s"]:
            watched(ha, cfg, p)
        measured = {}
        for e, (name, k) in subs.items():
            try:
                measured[name] = float(ha.http("/api/states/" + e)["state"]) * k
            except Exception:
                continue
            hist[name] = [x for x in hist.get(name, []) if now - x[0] < 120] + [(now, measured[name])]
        if subs:      # linea a monte dedotta: presa e linea che salgono insieme
            try:
                parents = where_parents(where, ctx[2])
                lpend, llast = st.setdefault("line_pend", {}), st.setdefault("line_last", {})
                for name in measured:
                    if name in parents or name == subs.get(cfg["profiler"], (None,))[0] or now - llast.get(name, 0) < 120:
                        continue
                    dw = plug_rise(hist.get(name, []), now)
                    if dw:
                        lpend[name], llast[name] = [now, dw], now
                for name, (t0, dw) in list(lpend.items()):
                    if now - t0 < 20:
                        continue
                    del lpend[name]
                    asked = st.setdefault("line_asked", [])
                    for line in line_vote(st.setdefault("line_votes", {}), hist, name, t0, dw):
                        if name + ">" + line in asked or name in parents:
                            continue
                        asked.append(name + ">" + line)
                        if tg_on:
                            ha.ask(*line_question(st, name, line))
                        else:
                            ha.notify(T("🔌 La presa %s sembra stare sulla linea %s (salite insieme %d volte). Se è "
                                      "giusto, indicalo in Impara → 📍 Dove sta.") % (name, line, LINE_VOTES))
                        break
                    jsave("state.json", st)
            except Exception as e:
                print("linee a monte:", e, flush=True)
        if ui_on:        # menu profili aggiornato quando cambiano (impronte + programmi/stati delle prese fisse)
            popts = profile_options(fps, progs, plug_levels(st))
            if popts != ui_opts:
                try:
                    ha.http("/api/services/input_select/set_options", {"entity_id": UI["fp"], "options": popts})
                    ui_opts = popts
                except Exception as e:
                    print("menu profili non aggiornato:", e, flush=True)
        if ui_on:      # scelta di un profilo da "I miei profili": precompila i campi per correggerlo
            try:
                v = ha.http("/api/states/" + UI["fp"])["state"]
                key = v.split(" ·")[0]
                if ui_seen.get("fpsel", v) != v and v != NO_FP:
                    if st.get("psess"):
                        ui_msg = T("❌ C'è un profilo in corso: i campi servono a lui. Scegli il profilo dopo Fine profilo.")
                    elif key.startswith("prog:"):
                        _, plug, pid = key.split(":", 2)
                        pr = next((p for p in progs.get(plug, []) if p["id"] == pid), None)
                        cat = plugs.get(plug, {})
                        ha.http("/api/services/input_text/set_value", {"entity_id": UI["nome"], "value": plug})
                        for k, ent in zip(CAT_FIELDS[:4], (UI_PROF["tipo"], UI_PROF["marca"], UI_PROF["modello"],
                                                           UI_PROF["codice"])):
                            set_field(ha, ent, cat.get(k, ""))
                        ha.http("/api/services/input_text/set_value",
                                {"entity_id": UI["stato"], "value": (pr or {}).get("name") or ""})
                        ui_msg = (T("✏️ %s (presa %s): il nome va nel campo Profilo, Tipo/Marca/Modello/Codice valgono "
                                  "per tutti i suoi programmi. Poi premi Modifica profilo.") % (pid, plug))
                    else:
                        fp = next((f for f in fps if key == "fp:" + f["id"]), None)
                        cat = (fp or {}).get("cat") or {}
                        ha.http("/api/services/input_text/set_value",
                                {"entity_id": UI["nome"], "value": (fp or {}).get("name") or ""})
                        for k, ent in zip(CAT_FIELDS, (UI_PROF["tipo"], UI_PROF["marca"], UI_PROF["modello"],
                                                       UI_PROF["codice"], UI["stato"])):
                            set_field(ha, ent, cat.get(k, ""))
                        ui_msg = (T("✏️ %s: correggi nome e/o dati del catalogo (solo se già misurata da una presa), "
                                  "poi premi Modifica profilo.") % (fp or {}).get("id", "?"))
                ui_seen["fpsel"] = v
            except Exception as e:
                print("selezione profilo non letta:", e, flush=True)
        for kind in ("white", "mark", "name") if ui_on else ():
            try:
                v = ha.http("/api/states/" + UI[kind])["state"]     # input_button = istante dell'ultima pressione
                if ui_seen.get(kind, v) != v:
                    txt = [ha.http("/api/states/" + UI[k])["state"].strip() for k in ("nome", "stato", "fp")]
                    dev, st_, fp_ = ["" if x in ("unknown", "unavailable") else x for x in txt]   # helper mai compilato
                    job = {"kind": kind, "t": now, "dev": dev, "st": st_, "fp": fp_}
                    if kind == "name":
                        job["cat"] = read_cat(ha)   # solo per le impronte già misurate: fp_edit valida il resto
                    ui_msg = {"white": T("⏳ Misuro il bianco: lascia tutto com'è per 2 minuti."),
                              "mark": T("⏳ Misuro %s: tienilo acceso e a regime per 1 minuto.") % (dev or "?"),
                              "name": "⏳"}[kind]
                ui_seen[kind] = v
            except Exception as e:
                print("pannello Impara non letto:", e, flush=True)
        if plug_on and plug_names(subs, cfg["profiler"]) != plug_opts:
            try:
                ha.http("/api/services/input_select/set_options",
                        {"entity_id": UI_PLUG["presa"], "options": plug_names(subs, cfg["profiler"])})
                plug_opts = plug_names(subs, cfg["profiler"])
            except Exception as e:
                print("menu prese non aggiornato:", e, flush=True)
        for kind in ("presa", "prog", "psave") if plug_on else ():
            try:
                v = ha.http("/api/states/" + UI_PLUG[kind])["state"]
                sel = v if kind == "presa" else ha.http("/api/states/" + UI_PLUG["presa"])["state"]
                changed = ui_seen.get(kind, v) != v
                if changed and kind != "psave" and st.get("psess"):
                    ui_msg = T("❌ C'è un profilo in corso: i campi servono a lui. Scegli la presa dopo Fine profilo.")
                elif changed and kind == "presa":      # mostra i dati salvati e i programmi, da correggere
                    cur = plugs.get(v, {})
                    for k, ent in zip(CAT_FIELDS, (UI_PROF["tipo"], UI_PROF["marca"], UI_PROF["modello"],
                                                   UI_PROF["codice"], UI["stato"])):
                        set_field(ha, ent, cur.get(k, ""))
                    prog_opts = None                    # il menu programmi si rifà sotto
                    ui_msg = "✏️ %s: %s" % (v, T("correggi i campi, scegli un programma e dagli il nome nel Profilo.")
                                            if cur else T("compila i campi e premi Salva dati presa."))
                elif changed and kind == "prog":       # Profilo = nome attuale del programma
                    p = next((p for p in progs.get(sel, []) if v.split(" ·")[0] == p["id"]), None)
                    ha.http("/api/services/input_text/set_value",
                            {"entity_id": UI["stato"], "value": (p or {}).get("name") or ""})
                elif changed:
                    ch = ha.http("/api/states/" + UI_PLUG["prog"])["state"]
                    ui_msg = plug_save(plugs, progs, sel, read_cat(ha), ch)
                    if ui_msg.startswith("✅"):
                        jsave("plugs.json", plugs)
                        jsave("programs.json", progs)
                        prog_opts = None
                        out = share(fps, plugs, progs, cfg, now, plug_levels(st))
                        ui_msg += " " + out if out else ""
                    ha.notify(ui_msg)
                ui_seen[kind] = v
                opts = prog_options(progs.get(v, []), plug_levels(st).get(v)) if kind == "presa" else None
                if opts and opts != prog_opts:
                    ha.http("/api/services/input_select/set_options", {"entity_id": UI_PLUG["prog"], "options": opts})
                    prog_opts = opts
                    # programma nuovo da nominare: selezionato, se no il nome finirebbe su quello scelto l'ultima
                    # volta (27/9: "PLA Drying" salvato sullo stato IDLE invece che sul ciclo appena finito)
                    new = prog_pick(opts, st.get("prog_ask"), v)
                    if new:
                        ha.http("/api/services/input_select/select_option", {"entity_id": UI_PLUG["prog"], "option": new})
                        set_field(ha, UI["stato"], "")
                        st.pop("prog_ask")
                    ui_seen["prog"] = ha.http("/api/states/" + UI_PLUG["prog"])["state"]
            except Exception as e:
                print("pannello prese non letto:", e, flush=True)
        while not tgq.empty():     # risposte da Telegram (pulsanti o testo scritto)
            ev = tgq.get()
            try:
                parts = ((ev.get("data") or {}).get("data") or "").split()
                q = st.get("asks", {}).get(parts[1] if len(parts) == 3 else st.get("ask_last"), {}).get("text")
                out = tg_handle(st, fps, where, ctx[0], ev)
                if out:
                    ha.tg_reply(ev, out, q)
                    jsave("fingerprints.json", fps)
                    jsave("where.json", where)
                    jsave("state.json", st)
            except Exception as e:
                print("risposta telegram non gestita:", e, flush=True)
        if where_on:     # "Dove sta?": stanza e linea di prese e impronte
            try:
                plugs_seen = [n for e, (n, _) in subs.items() if e != cfg["profiler"]]
                parents = where_parents(where, ctx[2])
                for kind, opts in (("cosa", where_options(fps, plugs_seen, jload("estimates.json", []))), ("stanza", [NO_AREA] + ctx[0]),
                                   ("linea", [ON_MAIN] + sorted(plugs_seen))):
                    if wopts.get(kind) != opts:
                        ha.http("/api/services/input_select/set_options", {"entity_id": UI_WHERE[kind], "options": opts})
                        wopts[kind] = opts
                v = ha.http("/api/states/" + UI_WHERE["cosa"])["state"]
                if ui_seen.get("wcosa", v) != v and v != NO_WHAT:     # precompila con quello che si sa già
                    k, _, nm = v.partition(" · ")
                    g = room_guess(next((f for f in fps if "fp:" + f["id"] == k), {}))
                    for kind, val in zip(("stanza", "linea"), where_now(where, k, ctx[1], parents,
                                                                         nm if k.startswith("fp:") else None, g)):
                        if val in wopts[kind]:
                            ha.http("/api/services/input_select/select_option",
                                    {"entity_id": UI_WHERE[kind], "option": val})
                ui_seen["wcosa"] = v
                b = ha.http("/api/states/" + UI_WHERE["salva"])["state"]
                if ui_seen.get("wsalva", b) != b:
                    ui_msg = where_save(where, v, ha.http("/api/states/" + UI_WHERE["stanza"])["state"],
                                        ha.http("/api/states/" + UI_WHERE["linea"])["state"], plugs_seen, parents)
                    if ui_msg.startswith("✅"):
                        jsave("where.json", where)
                ui_seen["wsalva"] = b
            except Exception as e:
                print("pannello Dove sta non letto:", e, flush=True)
        runs, idle, on_w = st.setdefault("runs", {}), st.setdefault("idle", {}), st.setdefault("on_w", {})
        for e, (name, _) in subs.items():      # cicli delle prese fisse → programmi
            if e == cfg["profiler"]:
                continue
            w = measured.get(name)
            runs[name], done = run_feed(runs.get(name), w, now, idle.get(name))
            if w is not None and runs[name] is None:
                idle[name] = idle_w(idle.get(name), w)
            if w is not None and idle.get(name) is not None and w >= idle[name] * 1.3 + PLUG_ON_W:
                on_w[name] = w if on_w.get(name) is None else on_w[name] + (w - on_w[name]) * 0.02   # consumo da acceso
            if done:
                p = prog_learn(progs.setdefault(name, []), done)
                jsave("programs.json", progs)
                prog_opts = None
                if p["name"] is None:     # da nominare: il menu Programma lo selezionerà da solo
                    st["prog_ask"] = {"plug": name, "id": p["id"]}
                ha.notify(run_msg(name, p, done, cfg["price_eur_kwh"]))
        if now - runs_saved > 60:
            jsave("state.json", st)        # un ciclo in corso sopravvive a un riavvio
            runs_saved = now
        for kind in ("pstart", "pstop") if prof_on else ():
            try:
                v = ha.http("/api/states/" + UI_PROF[kind])["state"]
                if ui_seen.get(kind, v) != v:
                    if kind == "pstart":
                        dev = ha.http("/api/states/" + UI["nome"])["state"].strip()
                        ui_msg = psess_start(st, "" if dev in ("unknown", "unavailable") else dev, cfg["profiler"], now,
                                             read_cat(ha))
                        if not cfg["profiler"]:
                            subs_at = 0     # la presa magari è appena stata rinominata: si ricerca subito
                    else:
                        ui_msg = psess_stop(st, fps, now, read_cat(ha), cfg["catalog_share"])
                    ha.notify(ui_msg)
                    out = send_outbox(cfg) if kind == "pstop" else None
                    if out:
                        ha.notify(out)
                ui_seen[kind] = v
            except Exception as e:
                print("pulsanti profiler non letti:", e, flush=True)
        if st.get("psess"):
            try:
                pw = float(ha.http("/api/states/" + cfg["profiler"])["state"])
            except Exception:
                pw = None       # presa staccata o sensore unavailable
            if psess_feed(st, pw, now):     # presa staccata: chiude e invia come Fine profilo
                ui_msg = psess_stop(st, fps, now, read_cat(ha), cfg["catalog_share"])
                ha.notify(ui_msg)
                out = send_outbox(cfg)
                if out:
                    ha.notify(out)
        if upd != last_upd:
            last_upd = upd
            det.floor = st["bases"][-1]["w"] if st["bases"] else None   # si aggiorna ogni notte
            log_sample(now, p)
            for ev in det.feed(now, p):
                if ev["type"] in ("on", "transient") and cfg["watch"]:
                    ev["hint"] = state_hint(watched(ha, cfg), cfg["watch"], ev["t"])
                if ev["type"] == "on":
                    if ctx[3]:     # cosa è cambiato nelle stanze in quel minuto
                        try:
                            on_clues[ev["t"]] = ev["stanze"] = room_clues(ha.http("/api/states"), ctx[3], ev["t"])
                        except Exception as e:
                            print("indizi delle stanze non letti:", e, flush=True)
                    ev["sub"] = who_stepped(hist, ev["t"], ev["w"])
                    if ev["sub"]:
                        on_names[ev["t"]] = ev["sub"]
                    if ev.get("hint"):
                        on_hints[ev["t"]] = ev["hint"]
                log_event(ev)
                if ev["type"] == "transient":
                    transients = [x for x in transients if now - x["t"] < 600] + [ev]
                if ev["type"] == "cycle":
                    sub = on_names.pop(ev["t"], None)
                    prof = subs.get(cfg["profiler"], (None,))[0]
                    if sub and sub == prof:
                        try:
                            typed = ha.http("/api/states/" + UI["nome"])["state"]
                        except Exception:
                            typed = None
                        sub = fp_name_from(sub, prof, typed)
                    clues = on_clues.pop(ev["t"], None)
                    if clues is not None and env and ev["min"] >= 10:   # sotto i 10 min una stanza non si muove
                        try:
                            ents = [e for k in env.values() for es in k.values() for e in es]
                            end = ev["t"] + ev["min"] * 60
                            clues = sorted(set(clues) | set(trend_clues(ha_history(ha, ents, ev["t"] - 1500, end),
                                                                        env, ev["t"], end)))
                        except Exception as e:
                            print("umidità/temperatura non lette:", e, flush=True)
                    areas = {f["id"]: where_get(where, "fp:" + f["id"], f["name"]).get("area") for f in fps}
                    fp = learn(fps, ev, sub, clues, areas, plug_names(subs, cfg["profiler"]))    # nome dalla presa che l'ha misurato
                    guess = room_guess(fp)
                    if guess and not areas.get(fp["id"]) and fp.get("guess_told") != guess:
                        fp["guess_told"] = guess
                        if tg_on:
                            ha.ask(*room_question(st, fp, guess))
                            jsave("state.json", st)
                        else:
                            ha.notify(T("📍 %s (+%s kW) sembra stare in %s: %s. Se è giusto, confermalo in Impara → "
                                      "📍 Dove sta.") % (fp["name"] or fp["id"], it(fp["w"] / 1000, 1), guess, ", ".join(
                                          k.split("|")[1] for k in fp["clues"] if k.startswith(guess + "|"))))
                    named = hint_name(fp, on_hints.pop(ev["t"], None))
                    if named and fp["name"] is None:
                        fp["name"] = named      # stato che coincide per la 2a volta
                        ha.notify(T("Ho riconosciuto un carico: %s ≈ %s kW per circa %d min.") % (
                            named, it(fp["w"] / 1000, 1), round(fp["min"])))
                    jsave("fingerprints.json", fps)
            if job:
                done = ui_job(job, fps, now, progs, plugs)
                if done:
                    ui_msg, job = done, None
                    ha.notify(done)
            prof = subs.get(cfg["profiler"], (None,))[0]
            night_and_base(ha, cfg, st, now, p, {k: w for k, w in measured.items() if k != prof})
            d = datetime.fromtimestamp(now)
            if d.weekday() == DIGEST_DAY and d.hour >= DIGEST_HOUR and st.get("digest_day") != str(d.date()):
                st["digest_day"] = str(d.date())
                try:
                    kwh = sum(band_kwh(read_samples(now - 7 * 86400)).values())
                    text, ask = digest(st, fps, kwh, cfg["price_eur_kwh"], st["bases"])
                    for q in [q for q, a in st.get("asks", {}).items() if a["kind"] == "name"]:
                        ask_close(st, q)          # le domande vecchie: chiedo di nuovo solo le impronte che pesano
                    ha.notify(text)
                    for f in ask:
                        like = catalog_suggest(jload("catalog-remote.json", []), f)
                        if tg_on:
                            ha.ask(*name_question(st, f, like))
                    if ask and not tg_on:
                        ha.notify(T("Dai un nome a quelle senza nome nella dashboard Wattson → Impara."))
                except Exception as e:
                    print("riepilogo settimanale non riuscito:", e, flush=True)
                jsave("state.json", st)
            state, advice = light(fl["net"], cfg, st["light"], det.open, fps, now)   # contatore: solo prelievo
            areas = {f["id"]: where_get(where, "fp:" + f["id"], f["name"]).get("area") for f in fps}
            active = [{"cosa": label(match_fp(fps, o["w"], (now - o["t"]) / 60, on_clues.get(o["t"]), areas) or
                                     next((f for f in fps if abs(o["w"] - f["w"]) <= FP_TOL * f["w"]), None), o["w"]),
                       "kw": round(o["w"] / 1000, 2), "da": fmt_t(o["t"])} for o in det.open]
            if state != st["light"]:
                if state == "stop":
                    lines = [T("⚡ %s kW alle %s (limite %s kW)") % (it(fl["net"] / 1000), fmt_t(now), it(cfg["tolerance_kw"]))]
                    lines += [T("• %s: %s kW, acceso dalle %s") % (a["cosa"], it(a["kw"], 1), a["da"]) for a in active]
                    lines += [T("• carico breve: +%s kW alle %s") % (it(x["w"] / 1000, 1), fmt_t(x["t"])) for x in transients]
                    lines += [T("• misurato: %s %d W") % (n, w) for n, w in sorted(measured.items(), key=lambda x: -x[1]) if w >= 50]
                    lines.append(advice)
                    ha.notify("\n".join(lines))
                elif st["light"] == "stop":
                    ha.notify(T("Rientrato: %s kW. %s") % (it(fl["net"] / 1000), advice))
                st["light"] = state
                jsave("state.json", st)
            sun = None
            if cfg["grid_signed"] or cfg["export_entity"]:     # c'è un fotovoltaico: quando usare i carichi pesanti
                big = cfg["big_load_kw"] * 1000
                loads = {f["name"]: f["w"] for f in fps if f["name"] and f["w"] >= big}
                loads.update({n: w for n, w in st["on_w"].items() if w >= big})   # prese fisse: consumo da acceso
                ss = st.setdefault("sun", {})
                was = ss.get("state")
                if sun_step(ss, sun_now(fl, cfg), now, cfg["sun_min"] * 60) and "sun" in (was, ss["state"]):
                    day = time.strftime("%Y-%m-%d")
                    if ss["state"] == "sun" and ss.get("day") != day:
                        ss.update(day=day, n=0)
                    if ss["state"] != "sun" and ss.pop("told", None):     # chiude solo una finestra annunciata
                        ha.notify(T("🌥️ Finito il surplus di sole (immetti %s kW). %s") % (
                            it(fl["export"] / 1000, 1), sun_advice(ss["state"], fl, cfg, loads)))
                    elif ss["state"] == "sun" and ss["n"] < SUN_DAY_MAX:
                        ss.update(n=ss["n"] + 1, told=True)
                        ha.notify(sun_advice("sun", fl, cfg, loads))
                    jsave("state.json", st)
                sun = {"stato": ss["state"], "consiglio": sun_advice(ss["state"], fl, cfg, loads),
                       "batteria_pct": fl["soc"],
                       "batteria_kw": None if fl["bat"] is None else round(fl["bat"] / 1000, 2)}
            top, rows = meter_tree(measured, where_parents(where, ctx[2]))
            for r in rows:
                r["stanza"] = (where.get("plug:" + r["nome"]) or {}).get("area") or ctx[1].get(r["nome"])
            try:
                ha.publish(state, {"friendly_name": T("Wattson semaforo"), "icon": "mdi:traffic-light",
                                   "consiglio": advice, "potenza_kw": round(fl["net"] / 1000, 3),
                                   "margine_kw": round(cfg["tolerance_kw"] - fl["net"] / 1000, 2), "accesi": active,
                                   "consumo_kw": round(p / 1000, 3),
                                   "immissione_kw": round(fl["export"] / 1000, 3),
                                   "fv_kw": None if fl["pv"] is None else round(fl["pv"] / 1000, 3),
                                   "sole": sun,     # None senza fotovoltaico
                                   "carico_base_w": st["bases"][-1]["w"] if st["bases"] else None,
                                   "misurati": rows,     # annidati: ogni presa conta una volta sola
                                   "non_misurato_w": round(p - top),
                                   "osservati": [{"nome": n, "stato": s_[0]} for n, s_ in
                                                 ((cfg["watch"][e], v) for e, v in watch_cache.items())],
                                   "profili": jload("profiles.json", {}),
                                   "bianco_w": jload("learn.json", {}).get("white"),
                                   "apprendimento": ui_msg,
                                   "catalogo": catalog_view(catalog_display(jload("catalog-remote.json", []), catalog_load())),
                                   "da_verificare": [{"nome": f["name"], "kw": round(f["w"] / 1000, 2), "min": f["min"],
                                                      "stanza": where_get(where, "fp:" + f["id"], f["name"]).get("area")}
                                                     for f in to_verify(fps, [n for n, _ in subs.values()])]
                                   + [dict(e, stanza=(where.get("est:" + est_id(i, e)) or {}).get("area"))   # stime dal
                                      for i, e in enumerate(jload("estimates.json", []))],   # generale, es. prima della presa
                                   "profilo_in_corso": {"nome": st["psess"]["dev"], "da": fmt_t(st["psess"]["t0"])}
                                   if st.get("psess") else None,
                                   "profili_personali": all_profiles(fps, progs, plug_levels(st), where, ctx[1])})
            except Exception as e:
                print("publish fallito:", e, flush=True)
        STOP.wait(cfg["poll_s"])


def night_and_base(ha, cfg, st, now, p, plugs=None):
    """Campioni 02–05; alle 05 calcola il carico di base e avvisa se è salito.
    plugs: letture delle prese fisse, la loro media notturna dice quanta parte del fondo è misurata."""
    d = datetime.fromtimestamp(now)
    if 2 <= d.hour < 5:
        st["night"].append(round(p))
        for name, w in (plugs or {}).items():
            acc = st.setdefault("night_plugs", {}).setdefault(name, [0.0, 0])
            acc[0], acc[1] = acc[0] + w, acc[1] + 1
        st["night_day"] = d.date().isoformat()
        if cfg["base_skip"] and len(st["night"]) % 60 == 1:     # ~ogni 5 min
            try:
                states = {e: ha.http("/api/states/" + e)["state"] for e in cfg["base_skip"]}
                why = busy(states, cfg["base_skip"])
                if why:
                    st["night_skip"] = why
            except Exception as e:
                print("base_skip non letto:", e, flush=True)
    elif d.hour >= 5 and st["night"] and len(st["night"]) > 50:
        base = sorted(st["night"])[len(st["night"]) // 10]      # 10° percentile
        skip = st.pop("night_skip", None)
        st["night"] = []
        pl = {k: round(a / n) for k, (a, n) in st.pop("night_plugs", {}).items() if n and a / n >= 1}
        log_event({"type": "base", "t": now, "w": base, "skip": skip})
        if skip:            # notte non rappresentativa (es. stampante in stampa)
            jsave("state.json", st)
            return
        prev = st["bases"][-7:]
        st["bases"] = (st["bases"] + [{"day": st["night_day"], "w": base, "plugs": pl}])[-60:]
        jsave("state.json", st)
        if len(prev) >= 3:
            ref = statistics.median(b["w"] for b in prev)
            if base - ref >= BASE_ALERT_W:
                ha.notify(T("Carico di base stanotte: %d W, di solito %d W (+%d W ≈ %d €/anno se resta). "
                          "Qualcosa è rimasto acceso?") % (base, ref, base - ref,
                                                          eur_year(base - ref, cfg["price_eur_kwh"]))
                          + base_why(prev, pl))
    elif d.hour >= 5:
        st["night"] = []


def base_why(prev, plugs):
    """Le prese che di notte consumano più di prima: " Di cui: +18 W Stampante." ("" se nessuna).
    Prima = mediana delle notti precedenti in cui la presa era già misurata (0 W se assente quella notte)."""
    old = [b["plugs"] for b in prev if "plugs" in b]
    up = sorted(((w - (statistics.median(o.get(k, 0) for o in old) if old else 0), k) for k, w in plugs.items()),
                reverse=True)
    up = ["+%d W %s" % (dw, k) for dw, k in up if dw >= BASE_PLUG_W]
    return T(" Di cui: %s.") % ", ".join(up) if up else ""


def base_text(bases):
    """Carico di base di stanotte, quanto ne misurano le prese e il resto."""
    if not bases:
        return None
    b = bases[-1]
    pl = sorted(b.get("plugs", {}).items(), key=lambda x: -x[1])
    s = T("Carico di base: %d W") % b["w"]
    if len(bases) >= 4:      # come l'avviso notturno: mediana delle 7 notti prima
        s += T(" (di solito %d W)") % statistics.median(x["w"] for x in bases[-8:-1])
    if pl:
        s += T(": %d W dalle prese (%s), %d W non misurati") % (
            sum(w for _, w in pl), ", ".join("%s %d W" % x for x in pl), max(b["w"] - sum(w for _, w in pl), 0))
    return s + "."


def digest(st, fps, kwh, price, bases):
    """Riepilogo della settimana: (testo, impronte senza nome da chiedere). Energia per impronta = quella
    accumulata dall'ultimo riepilogo (dal primo avvio, la prima volta). Sotto DIGEST_MIN_KWH non si chiede."""
    first, snap = "digest_wh" not in st, st.get("digest_wh", {})
    week = {f["id"]: (fp_wh(f) - snap.get(f["id"], 0)) / 1000 for f in fps}
    st["digest_wh"] = {f["id"]: fp_wh(f) for f in fps}
    named = {}
    for f in fps:
        if f["name"] and week[f["id"]] > 0:
            named[f["name"]] = named.get(f["name"], 0) + week[f["id"]]
    unknown = sorted((f for f in fps if not f["name"] and week[f["id"]] >= DIGEST_MIN_KWH), key=lambda f: -week[f["id"]])
    lines = [T("📊 La settimana di Wattson: %s kWh (%s €), %s kWh al giorno.") % (
        it(kwh, 1), it(kwh * price), it(kwh / 7, 1))]
    lines += [base_text(bases)] if bases else []
    if named:
        lines.append(T("Riconosciuti %s:") % (T("finora") if first else T("in settimana")))
        lines += ["• %s: %s kWh" % (n, it(k, 1)) for n, k in sorted(named.items(), key=lambda x: -x[1])[:6]]
    if unknown:
        lines.append(T("Senza nome, i più pesanti:"))
        lines += [T("• %s: +%s kW per circa %d min, %s kWh") % (f["id"], it(f["w"] / 1000, 1), round(f["min"] or 0),
                                                              it(week[f["id"]], 1)) for f in unknown[:5]]
    return "\n".join(lines), unknown[:DIGEST_ASK]


def fill_dashboard(tpl, power, unit, cfg, logo_url):
    """Modello dashboard.json → config Lovelace: sensore, soglie del gauge nell'unità del sensore, logo."""
    k = 1000 if unit == "W" else 1
    val = {"{power}": power, "{max}": math.ceil(cfg["tolerance_kw"]) * k,
           "{yellow}": round((cfg["tolerance_kw"] - cfg["big_load_kw"]) * k, 2), "{red}": cfg["limit_kw"] * k}

    def walk(x):
        if isinstance(x, dict):
            return {a: walk(b) for a, b in x.items()}
        if isinstance(x, list):
            return [walk(b) for b in x]
        return DASH_TR.get(x, val.get(x, x)) if isinstance(x, str) else x
    out = walk(tpl)
    ensure_logo(out, logo_url)
    return out


def ensure_logo(dash, logo_url):
    """Logo come prima card della prima vista (sostituisce un logo vecchio, non duplica)."""
    cards = dash["views"][0].setdefault("cards", [])
    pic = {"type": "picture", "image": logo_url, "alt_text": "Wattson"}
    if cards and cards[0].get("type") == "picture" and cards[0].get("alt_text") == "Wattson":
        cards[0] = pic
    else:
        cards.insert(0, pic)


def ensure_view(dash, view):
    """Aggiunge una vista del modello se manca (per path); non tocca quelle esistenti."""
    if not any(v.get("path") == view["path"] for v in dash["views"]):
        dash["views"].append(view)


def ensure_helpers(ha, ws):
    """Crea gli helper del pannello "Impara" che mancano (il nome dà l'entity_id)."""
    have = {s["entity_id"] for s in ha.http("/api/states")}
    for key, name, icon in (("nome", "Wattson apparecchio", "mdi:tag"), ("stato", "Wattson stato", "mdi:tag-text"),
                            ("white", "Wattson misura bianco", "mdi:power-sleep"),
                            ("mark", "Wattson segna impronta", "mdi:fingerprint"),
                            ("fp", "Wattson impronta", "mdi:fingerprint"), ("name", "Wattson dai nome", "mdi:rename"),
                            ("pstart", "Wattson inizia profilo", "mdi:record-rec"),
                            ("pstop", "Wattson fine profilo", "mdi:stop-circle-outline"),
                            ("tipo", "Wattson tipo", "mdi:shape"), ("marca", "Wattson marca", "mdi:factory"),
                            ("modello", "Wattson modello", "mdi:tag-outline"),
                            ("codice", "Wattson codice prodotto", "mdi:barcode"),
                            ("presa", "Wattson presa", "mdi:power-socket-eu"),
                            ("prog", "Wattson programma", "mdi:format-list-numbered"),
                            ("psave", "Wattson salva presa", "mdi:content-save"),
                            ("cosa", "Wattson dove cosa", "mdi:map-marker-question"),
                            ("stanza", "Wattson stanza", "mdi:floor-plan"),
                            ("linea", "Wattson linea", "mdi:transmission-tower"),
                            ("salva", "Wattson salva dove", "mdi:map-marker-check")):
        ent = {**UI, **UI_PROF, **UI_PLUG, **UI_WHERE}[key]
        domain = ent.split(".")[0]
        if ent not in have:
            extra = {"input_text": {"min": 0, "max": 100},
                     "input_select": {"options": type_options() if key == "tipo" else [
                         {"presa": NO_PLUG, "prog": NO_PROG, "cosa": NO_WHAT, "stanza": NO_AREA, "linea": ON_MAIN}.get(key, NO_FP)]}
                     }.get(domain, {})
            r = ws.call(type=domain + "/create", name=name, icon=icon, **extra)
            if not r["success"] or r["result"]["id"] != ent.split(".")[1]:
                sys.exit("helper %s non creato: %s" % (ent, r.get("error") or r["result"]))


class WS:
    """Client WebSocket minimo per HA (serve solo per le dashboard: via REST non si possono creare)."""

    def __init__(self, url, token):
        u = urllib.parse.urlsplit(url)
        s = socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=30)
        self.s = ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname) if u.scheme == "https" else s
        self.s.sendall(("GET /api/websocket HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                        % (u.netloc, base64.b64encode(os.urandom(16)).decode())).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket rifiutato: " + head.split(b"\r\n")[0].decode())
        self.n = 0
        self.recv()                                   # auth_required
        self.send({"type": "auth", "access_token": token})
        if self.recv()["type"] != "auth_ok":
            raise RuntimeError("token HA non valido")

    def _read(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise RuntimeError("connessione chiusa")
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        p = json.dumps(obj).encode()
        n = len(p)
        h = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack(">H", n)
                             if n < 65536 else bytes([0xFF]) + struct.pack(">Q", n))
        m = os.urandom(4)
        self.s.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

    def recv(self):
        data = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b0 & 0x0F == 0x9:          # ping di HA: si risponde pong, se no chiude la connessione
                m = os.urandom(4)
                self.s.sendall(bytes([0x8A, 0x80 | len(payload)]) + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload)))
                continue
            if b0 & 0x0F == 0x8:
                raise RuntimeError("websocket chiuso da HA")
            if b0 & 0x0F == 0xA:
                continue
            data += payload
            if b0 & 0x80:
                return json.loads(data)

    def call(self, **cmd):
        self.n += 1
        self.send(dict(cmd, id=self.n))
        while True:
            r = self.recv()
            if r.get("id") == self.n:
                return r


def upload_logo(ha):
    """Carica logo/wattson-logo.png in HA (una volta sola) e ne restituisce l'URL."""
    known = jload("logo.json", {}).get("id")
    if known:
        try:
            urllib.request.urlopen(ha.cfg["ha_url"] + "/api/image/serve/%s/original" % known, timeout=20)
            return "/api/image/serve/%s/original" % known
        except urllib.error.HTTPError:
            pass                                      # cancellato da HA: si ricarica
    png = open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "logo", "wattson-logo.png"), "rb").read()
    b = "wattson" + os.urandom(8).hex()
    body = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"wattson-logo.png\"\r\n"
            "Content-Type: image/png\r\n\r\n" % b).encode() + png + ("\r\n--%s--\r\n" % b).encode()
    req = urllib.request.Request(ha.cfg["ha_url"] + "/api/image/upload", body, {
        "Authorization": "Bearer " + ha.token, "Content-Type": "multipart/form-data; boundary=" + b})
    with urllib.request.urlopen(req, timeout=60) as r:
        known = json.loads(r.read())["id"]
    jsave("logo.json", {"id": known})
    return "/api/image/serve/%s/original" % known


def dashboard(cfg, force):
    """Crea la dashboard Wattson con il logo; se esiste già aggiorna solo il logo (--force la riscrive)."""
    ha = HA(cfg, dry=False)
    set_lang(ha_lang(ha, cfg))
    logo = upload_logo(ha)
    url = cfg["dashboard"]
    ws = WS(cfg["ha_url"], ha.token)
    ensure_helpers(ha, ws)
    if not any(d["url_path"] == url for d in ws.call(type="lovelace/dashboards/list")["result"]):
        r = ws.call(type="lovelace/dashboards/create", url_path=url, title="Wattson",
                    icon="mdi:magnify", show_in_sidebar=True, require_admin=False, mode="storage")
        if not r["success"]:
            sys.exit("dashboard non creata: %s" % r["error"]["message"])
    cur = ws.call(type="lovelace/config", url_path=url)
    unit = ha.http("/api/states/" + cfg["power_entity"])["attributes"].get("unit_of_measurement", "W")
    if cur["success"] and cur["result"].get("views") and not force:
        dash, what = cur["result"], T("logo e viste nuove aggiornati")
        ensure_logo(dash, logo)
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "dashboard.json")) as f:
            for v in fill_dashboard(json.load(f), cfg["power_entity"], unit, cfg, logo)["views"]:
                ensure_view(dash, v)
    else:
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "dashboard.json")) as f:
            dash, what = fill_dashboard(json.load(f), cfg["power_entity"], unit, cfg, logo), T("dashboard creata")
    r = ws.call(type="lovelace/config/save", url_path=url, config=dash)
    if not r["success"]:
        sys.exit("salvataggio fallito: %s" % r["error"]["message"])
    print("ok: %s → %s/%s" % (what, cfg["ha_url"], url))


def main(argv):
    if argv[:1] == ["dashboard"]:
        return dashboard(load_cfg(), "--force" in argv)
    if argv[:1] == ["fp"]:
        for fp in jload("fingerprints.json", []):
            print("%-5s %-18s +%.2f kW  %5.1f min  visto %d volte" % (
                fp["id"], fp["name"] or "(senza nome)", fp["w"] / 1000, fp["min"], fp["n"]))
        return
    if argv[:1] == ["name"] and len(argv) >= 3:
        fps = jload("fingerprints.json", [])
        for fp in fps:
            if fp["id"] == argv[1]:
                fp["name"] = " ".join(argv[2:])
                jsave("fingerprints.json", fps)
                print("ok:", fp["id"], "=", fp["name"])
                return
        sys.exit("impronta non trovata: " + argv[1])
    if argv[:1] == ["learn"]:
        return learn_cli(argv[1:])
    if argv[:1] == ["offerte"]:     # bolletta contro le offerte del Portale Offerte ARERA di oggi
        cfg = load_cfg()
        if not cfg["bill"]:
            sys.exit("Manca \"bill\" in config.json: vedi il manuale, sezione bolletta.")
        set_lang(ha_lang(HA(cfg, True), cfg))
        st = jload("state.json", {})
        r, cons = offers_check(cfg, st["bases"][-1]["w"] if st.get("bases") else None)
        return print(savings_msg(r, cons))
    if argv[:1] == ["telegram"]:    # bot Telegram guidato: BotFather → chat → HA → notify_entity
        return telegram_setup(load_cfg())
    if argv[:1] == ["github"]:      # collega GitHub una volta (per inviare al catalogo condiviso)
        return github_login(load_cfg())
    if argv[:1] == ["catalog"]:     # rimette nel catalogo le impronte con tipo (anche quelle corrette a mano)
        cat = catalog_merge(catalog_load(), jload("fingerprints.json", []), install_id(),
                            datetime.now().date().isoformat())
        catalog_save(cat)
        for e in cat:
            print("%-16s %-12s %-14s %-12s %-16s +%.2f kW %5.1f min  visto %d volte in %d case" % (
                e["type"], e["brand"], e["model"], e["code"], e["profile"], e["w"] / 1000, e["min"], e["n"], e["homes"]))
        return
    run(dry="--dry-run" in argv)


def learn_white(now):
    """Bianco = consumo stabile degli ultimi 2 min. Ritorna (ok, messaggio)."""
    w = steady(read_samples(now - 130), now, 120)
    if w is None:
        return False, T("Non stabile (o pochi dati): aspetta 2 minuti con tutto fermo e riprova.")
    jsave("learn.json", {"white": round(w), "t": now})
    return True, T("Bianco: %d W. Ora accendi un dispositivo alla volta.") % w


def learn_mark(fps, dev, st, now):
    """Consumo stabile dell'ultimo minuto − bianco → profilo (e impronta se riconoscibile)."""
    lj = jload("learn.json", {})
    if "white" not in lj:
        return False, T("Prima misura il bianco.")
    w = steady(read_samples(now - 70), now, 60, max_spread=80)
    if w is None:
        return False, T("Consumo non stabile: aspetta che il dispositivo sia a regime e riprova.")
    dw, prof = w - lj["white"], jload("profiles.json", {})
    prof.setdefault(dev, {})[st] = round(dw)
    jsave("profiles.json", prof)
    msg = "%s / %s: %d W" % (dev, st, dw)
    if dw >= STEP_W:          # abbastanza grande da riconoscerlo sul generale
        fp = seed_fp(fps, dev if st in ("on", "acceso") else "%s (%s)" % (dev, st), dw)
        jsave("fingerprints.json", fps)
        msg += T(" → impronta %s \"%s\"") % (fp["id"], fp["name"])
    return True, msg + T(". Spegnilo prima del prossimo dispositivo.")


# --- sessione con la presa "profiler": impara un apparecchio senza spegnere l'impianto -----
PLUG_ON_W = 5        # sopra questa soglia la presa sta alimentando qualcosa
PSESS_MAX = 8 * 3600  # una sessione dimenticata si chiude da sola
PSESS_GONE = 120      # presa staccata (unavailable) da tanto = fine sessione


def _cycles(samples, floor=None):
    """Cicli (acceso → spento) di una serie; quelli ancora accesi si chiudono all'ultimo campione."""
    d, out = Detector(floor), []
    for t, w in samples:
        out += [e for e in d.feed(t, w) if e["type"] == "cycle"]
    if samples:
        t = samples[-1][0]
        out += [{"type": "cycle", "t": o["t"], "end": t, "w": round(o["w"]), "min": round((t - o["t"]) / 60, 1)}
                for o in d.open]
    return out


def profile_session(dev, plug, main, floor=None):
    """Dalla curva pulita della presa (plug) e dal generale nella stessa finestra (main) → profilo e impronte.
    Ogni blocco ≥ STEP_W sulla presa cerca il suo gradino sul generale: i watt dell'impronta sono quelli
    del generale (è lì che Wattson dovrà riconoscerlo), la durata quella della presa.
    Ritorna (profilo {stato: W}, [{"name", "w", "min", "n", "seen"}], riassunto)."""
    on = [w for _, w in plug if w >= PLUG_ON_W]
    if not on:
        return None, [], T("la presa non ha misurato niente: l'apparecchio era attaccato e acceso?")
    kwh = sum(a[1] * (b[0] - a[0]) for a, b in zip(plug, plug[1:])) / 3.6e6
    act = [t for t, w in plug if w >= PLUG_ON_W]
    info = T("%d min, %s kWh, picco %d W") % (round((act[-1] - act[0]) / 60), it(kwh, 2), max(on))
    prof = {"picco": round(max(on)), "medio": round(statistics.mean(on)), "kwh": round(kwh, 3)}
    mains = _cycles(main, floor)
    groups = []                     # blocchi della presa con la stessa potenza = stessa impronta
    for c in _cycles(plug):
        m = min((m for m in mains if abs(m["t"] - c["t"]) <= 90 and abs(m["w"] - c["w"]) <= MATCH_TOL * c["w"]),
                key=lambda m: abs(m["w"] - c["w"]), default=None)
        c["main_w"] = m["w"] if m else None
        g = next((g for g in groups if abs(g[0]["w"] - c["w"]) <= FP_TOL * g[0]["w"]), None)
        (g.append(c) if g else groups.append([c]))
    out = []
    for g in groups:
        seen = [c["main_w"] for c in g if c["main_w"]]
        w = round(statistics.mean(seen or [c["w"] for c in g]))
        out.append({"name": dev if len(groups) == 1 else "%s (%s kW)" % (dev, it(w / 1000, 1)), "w": w,
                    "min": round(statistics.mean(c["min"] for c in g), 1), "n": len(g), "seen": len(seen),
                    "kwh": round(kwh, 3), "avg_w": prof["medio"]})   # consumo vero (presa), w = gradino sul generale
    return prof, out, info


def save_profile(fps, dev, prof, found, cat=None, already_on=None):
    """Scrive profilo e impronte trovate da una sessione. Ritorna il testo dell'esito. already_on = W della presa
    al primo campione se l'apparecchio era già acceso (allora sul generale non c'è nessun gradino da vedere)."""
    if prof is not None:
        allp = jload("profiles.json", {})
        allp[dev] = prof
        jsave("profiles.json", allp)
    parts = []
    for f in found:
        fp = next((x for x in fps if x["name"] == f["name"]), None) or \
            next((x for x in fps if x["name"] is None and x is match_fp(fps, f["w"], f["min"])), None)
        if fp is None:
            fp = {"id": "fp%d" % (len(fps) + 1), "seen": []}
            fps.append(fp)
        fp.update(name=f["name"], w=f["w"], min=f["min"], n=max(fp.get("n", 0), f["n"]), profiler=True,
                  **{k: f[k] for k in CAT_REAL if k in f})
        if cat and cat.get("type"):
            fp["cat"] = cat
        parts.append(T("impronta %s \"%s\": +%s kW per ~%d min, %d blocc%s, sul generale %d/%d") % (
            fp["id"], f["name"], it(f["w"] / 1000, 1), round(f["min"]), f["n"], "o" if f["n"] == 1 else "hi",
            f["seen"], f["n"]))
    if found:
        jsave("fingerprints.json", fps)
    elif prof is not None:
        fp = next((x for x in fps if x["name"] == dev), None)
        if fp is None:
            fp = {"id": "fp%d" % (len(fps) + 1), "seen": []}
            fps.append(fp)
        fp.update(name=dev, w=0, min=0, n=fp.get("n", 0) + 1, profiler=True, kwh=prof["kwh"], avg_w=prof["medio"])
        if cat and cat.get("type"):
            fp["cat"] = cat
        jsave("fingerprints.json", fps)
        if already_on:
            parts.append(T("era già acceso all'inizio (%d W): sul generale non c'è nessun gradino da vedere. Il profilo "
                         "dei consumi è valido (%s); per l'impronta rifai partendo da spento") % (already_on, fp["id"]))
        else:
            parts.append(T("nessun blocco da %d W in su: troppo piccolo per riconoscerlo sul generale, salvo il profilo "
                         "(%s)") % (STEP_W, fp["id"]))
    return "; ".join(parts)


def psess_file():
    return os.path.join(DATA, "profiler-session.csv")




def psess_start(st, dev, profiler, now, cat=None):
    if not profiler:
        return (T("❌ Non trovo la presa Profiler. Collega una smart plug con misura dei consumi, aggiungila a Home "
                "Assistant e rinominala \"Profiler\", poi premi di nuovo Inizia profilo."))
    missing = ([] if dev else [T("Nome device")]) + [CAT_LABELS[k] for k in CAT_FIELDS if not (cat or {}).get(k)]
    if missing:   # tutti obbligatori: il catalogo condiviso vale quanto i dati che ci entrano
        return T("❌ Compila tutti i campi prima di iniziare. Mancano: %s.") % ", ".join(missing)
    if st.get("psess"):
        return T("❌ C'è già un profilo in corso: %s. Premi Fine profilo.") % st["psess"]["dev"]
    os.makedirs(DATA, exist_ok=True)
    open(psess_file(), "w").close()
    st["psess"] = {"dev": dev, "t0": now, "gone": None, "cat": cat}
    jsave("state.json", st)
    return (T("⏺ Profilo di %s in corso. Attacca %s alla presa profiler (meglio se ancora spento), usalo "
            "normalmente fino alla fine del ciclo, poi premi Fine profilo o stacca la presa.") % (dev, dev))


def psess_feed(st, w, now):
    """Un campione della presa (None = non disponibile). True = la sessione va chiusa."""
    ps = st["psess"]
    if w is None:
        if ps["gone"] is None:
            ps["gone"] = now
            jsave("state.json", st)
        return now - ps["gone"] > PSESS_GONE
    ps["gone"] = None
    with open(psess_file(), "a") as f:
        f.write("%d,%.1f\n" % (now, w))
    return now - ps["t0"] > PSESS_MAX


def psess_stop(st, fps, now, cat=None, share=False):
    """Chiude la sessione. cat = campi letti ALLA CHIUSURA (correggibili durante); vuoti → quelli dell'avvio."""
    ps = st.pop("psess", None)
    jsave("state.json", st)
    if not ps:
        return T("❌ Nessun profilo in corso: premi prima Inizia profilo.")
    try:
        with open(psess_file()) as f:
            plug = [(int(a), float(b)) for a, b in (l.strip().split(",") for l in f if l.strip())]
    except OSError:
        plug = []
    main = read_samples(ps["t0"] - 120)
    floor = st["bases"][-1]["w"] if st.get("bases") else None
    cat = {k: (cat or {}).get(k) or (ps.get("cat") or {}).get(k, "") for k in CAT_FIELDS}
    prof, found, info = profile_session(ps["dev"], plug, main, floor)
    if prof is None:
        return "❌ %s: %s" % (ps["dev"], info)
    tag = " · ".join(v for v in (cat or {}).values() if v)
    msg = "✅ %s%s: %s. %s." % (ps["dev"], " (%s)" % tag if tag else "", info, save_profile(fps, ps["dev"], prof, found, cat,
                                                                          round(plug[0][1]) if plug[0][1] >= STEP_W else None))
    if cat and cat.get("type"):
        names = {f["name"] for f in found} or {ps["dev"]}
        catalog_save(catalog_merge(catalog_load(), fps, install_id(), datetime.fromtimestamp(now).date().isoformat()))
        c = contribution(fps, names)
        if share and c:
            jsave("outbox.json", jload("outbox.json", []) + [c])
            for f in fps:
                if f["name"] in names:
                    f["sent_n"] = f["n"]
            jsave("fingerprints.json", fps)
            msg += T(" La invio al catalogo condiviso.")
        else:
            msg += T(" Condivisione spenta: resta nel tuo catalogo (attivala per aiutare gli altri Wattson).")
    elif found:
        msg += T(" Scrivi il Tipo per metterla nel catalogo condiviso.")
    return msg


# --- catalogo condiviso: impronte per tipo · marca · modello · profilo, senza dati della casa ---------
CAT_FIELDS = ("type", "brand", "model", "code", "profile")   # schema del catalogo pubblico (in inglese)
CAT_REAL = ("kwh", "avg_w")    # facoltativi, misurati dalla presa: quanto consuma davvero (w = come si riconosce)


def read_cat(ha):
    """Campi del catalogo dal pannello (vuoti se mai compilati)."""
    out = {}
    for k, e in (("type", UI_PROF["tipo"]), ("brand", UI_PROF["marca"]), ("model", UI_PROF["modello"]),
                 ("code", UI_PROF["codice"]), ("profile", UI["stato"])):
        try:
            v = ha.http("/api/states/" + e)["state"].strip()
        except Exception:
            v = ""
        out[k] = "" if v in ("unknown", "unavailable", NO_TYPE) else v
    out["type"] = type_key(out["type"])
    return out


def install_id():
    """Id anonimo di questa installazione: ogni casa aggiorna solo il proprio contributo."""
    j = jload("install.json", {})
    if "id" not in j:
        j["id"] = base64.urlsafe_b64encode(os.urandom(6)).decode()
        jsave("install.json", j)
    return j["id"]


def catalog_load():
    """Le impronte di questa casa nel formato del catalogo (quello condiviso è catalog-remote.json)."""
    return jload("catalog-mine.json", [])


def catalog_save(cat):
    jsave("catalog-mine.json", cat)


def catalog_display(remote, mine):
    """Catalogo condiviso + le proprie voci che lì non ci sono ancora (homes 0 = solo tua)."""
    key = lambda d: tuple((d.get(k) or "").strip().lower() for k in CAT_FIELDS)
    out = list(remote)
    for m in mine:
        if not any(key(r) == key(m) and abs(r["w"] - m["w"]) <= FP_TOL * r["w"] for r in remote):
            out.append(dict(m, homes=0))
    return sorted(out, key=key)


def catalog_suggest(entries, fp, k=2):
    """Voci del catalogo che somigliano a un'impronta senza nome (stessa potenza e durata compatibile)."""
    near = [e for e in entries if fp.get("min") and abs(e["w"] - fp["w"]) <= FP_TOL * e["w"]
            and e["min"] / 2.5 <= fp["min"] <= e["min"] * 2.5]
    near.sort(key=lambda e: (-e.get("homes", 0), abs(e["w"] - fp["w"])))
    return [" ".join(x for x in (e["type"], e["brand"], e["model"], "(%s)" % e["profile"] if e["profile"] else "")
                     if x) for e in near[:k]]


def contribution(fps, names):
    """Issue per il catalogo condiviso con le impronte appena imparate: solo i campi del catalogo."""
    voci = [dict({k: f["cat"].get(k, "") for k in CAT_FIELDS}, w=f["w"], min=f["min"], n=f["n"],
                 **{k: f[k] for k in CAT_REAL if f.get(k)})
            for f in fps if f["name"] in names and (f.get("cat") or {}).get("type") and f.get("min") is not None]
    if not voci:
        return None
    v = voci[0]
    return {"title": "Fingerprint: " + " · ".join(x for x in (v["type"], v["brand"], v["model"], v["code"], v["profile"]) if x),
            "body": "Contribution sent by Wattson (learned with a metering plug, cross-checked against the main meter)."
                    "\n\n```json\n%s\n```\n" % json.dumps({"wattson": 1, "entries": voci}, ensure_ascii=False)}


# --- GitHub: collegamento una tantum (device flow, come HACS) e invio dei contributi ---------------
def _gh(url, data, token=None):
    """POST a GitHub: form per il login, JSON (con token) per le API."""
    if token:
        body, ctype = json.dumps(data).encode(), "application/json"
    else:
        body, ctype = urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", "Content-Type": ctype,
                                                           "User-Agent": "wattson"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def github_login(cfg):
    cid = cfg["github_client_id"]
    if not cid:
        sys.exit("Manca github_client_id in config: serve l'app GitHub del catalogo.")
    d = _gh("https://github.com/login/device/code", {"client_id": cid})
    print("Apri %s e scrivi il codice: %s" % (d["verification_uri"], d["user_code"]), flush=True)
    wait, end = d.get("interval", 5), time.time() + d["expires_in"]
    while time.time() < end:
        time.sleep(wait)
        err = gh_poll(cid, d["device_code"])
        if err is None:
            return print("✅ GitHub collegato: Wattson può inviare le impronte al catalogo condiviso.")
        if err == "slow_down":
            wait += 5
        elif err != "authorization_pending":
            sys.exit(err)
    sys.exit("Codice scaduto: rilancia wattson.py github.")


def gh_poll(cid, device_code):
    """Il codice è stato autorizzato su GitHub? None = sì (token salvato), se no l'errore di GitHub
    (authorization_pending, slow_down, expired_token, access_denied)."""
    r = _gh("https://github.com/login/oauth/access_token", {
        "client_id": cid, "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if "access_token" in r:
        gh_save(r, time.time())
        return None
    return r.get("error") or "?"


def gh_linked(cfg):
    """Collegamento a GitHub ancora buono (rinnova il token se serve): no = Configura mostra un codice nuovo."""
    return bool(gh_token(cfg, time.time()))


# --- bolletta e offerte (Italia): fasce ARERA dai consumi misurati, confronto col Portale Offerte -------------
# Formule: "Regole per il calcolo della Spesa" del SII (v3.02), solo materia energia: quota energia per fascia
# (+ spread e PUN×(1+λ) se indicizzata), commercializzazione e quote fisse dell'offerta. Dispacciamento, rete,
# oneri e imposte sono uguali per tutte le offerte e restano fuori.

PO_URL = "https://www.ilportaleofferte.it"
PO_NS = "{http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01}"
BANDS = ("F1", "F2", "F3")
BANDS_TYPICAL = {"F1": 0.33, "F2": 0.31, "F3": 0.36}   # quote di una casa media, finché non ci sono 7 giorni misurati
OFFER_SAVE_MIN = 30      # € l'anno: sotto, non vale l'avviso
RESERVED = re.compile(r"riservat|convenzion|dipendent|collaborator|\bsoci\b|associat|iscritt|tesserat|cral\b|"
                      r"esclusiv|dedicat", re.I)
REGIONS = ("Piemonte", "Valle d'Aosta", "Lombardia", "Trentino-Alto Adige", "Veneto", "Friuli-Venezia Giulia", "Liguria",
           "Emilia-Romagna", "Toscana", "Umbria", "Marche", "Lazio", "Abruzzo", "Molise", "Campania", "Puglia",
           "Basilicata", "Calabria", "Sicilia", "Sardegna")      # in ordine di codice ISTAT, 01–20


def region_code(v):
    """Regione dal nome ("lombardia") o dal codice ISTAT ("03", 3) → "03"; None se non c'è."""
    if v is None or v == "":
        return None
    if str(v).isdigit():
        return "%02d" % int(v)
    t = str(v).strip().lower()
    return next(("%02d" % (i + 1) for i, r in enumerate(REGIONS) if r.lower().startswith(t[:5])), None)


def in_zone(o, region=None, comune=None):
    """Offerta nazionale (nessuna zona) o di una zona che comprende la casa: in ogni zona vale il campo più preciso
    (comune ISTAT a 6 cifre, provincia = le sue prime 3, regione)."""
    zones = o.findall(PO_NS + "ZoneOfferta")
    if not zones:
        return True
    for z in zones:
        c, p, r = (z.findtext(PO_NS + k) for k in ("COMUNE", "PROVINCIA", "REGIONE"))
        if c and comune and c == comune or not c and p and comune and p == comune[:3] or not c and not p and r and r == region:
            return True
    return False


def easter(y):
    """Domenica di Pasqua (algoritmo di Gauss/Meeus)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l_) // 433
    month = (h + l_ - 7 * m + 90) // 25
    return date(y, month, (h + l_ - 7 * m + 33 * month + 19) % 32)


def band(dt):
    """Fascia ARERA di un istante: F1 lun–ven 8–19; F2 lun–ven 7–8 e 19–23, sabato 7–23; F3 il resto, domeniche e
    festivi nazionali (con Pasquetta)."""
    d = dt.date()
    hol = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    if d.weekday() == 6 or (d.month, d.day) in hol or d == easter(d.year) + timedelta(days=1) or not 7 <= dt.hour < 23:
        return "F3"
    return "F1" if d.weekday() < 5 and 8 <= dt.hour < 19 else "F2"


def band_kwh(samples, max_gap=300):
    """kWh per fascia da campioni (t, W); un buco oltre max_gap secondi non conta."""
    out = dict.fromkeys(BANDS, 0.0)
    for (t0, w0), (t1, _) in zip(samples, samples[1:]):
        if 0 < t1 - t0 <= max_gap:
            out[band(datetime.fromtimestamp(t0))] += w0 * (t1 - t0) / 3.6e6
    return out


def band_split(kwh_year, measured, days):
    """Consumo annuo diviso per fascia: con le quote misurate (≥ 7 giorni), se no quelle tipiche."""
    tot = sum(measured.values())
    share = {b: measured[b] / tot for b in BANDS} if days >= 7 and tot > 0 else BANDS_TYPICAL
    if not kwh_year:
        kwh_year = tot * 365 / days if days >= 7 else None
    return {b: kwh_year * share[b] for b in BANDS} if kwh_year else None


def parse_offers(xml_bytes, kwh_year, resident=True, today=None, region=None, comune=None):
    """Offerte luce domestiche del mercato libero valide oggi, sola luce, senza condizioni limitanti, per il consumo
    dato. Ognuna: {name, url, var, F1/F2/F3 €/kWh, vol €/kWh su tutto, fix €/anno, pot €/kW/anno}."""
    import xml.etree.ElementTree as ET
    today = today or date.today()
    n = lambda e, p: e.findtext(PO_NS + p.replace("/", "/" + PO_NS))
    out = []
    for o in ET.fromstring(xml_bytes):
        if (n(o, "DettaglioOfferta/TIPO_MERCATO"), n(o, "DettaglioOfferta/TIPO_CLIENTE"),
                n(o, "DettaglioOfferta/OFFERTA_SINGOLA")) != ("01", "01", "SI"):
            continue
        if n(o, "DettaglioOfferta/DOMESTICO_RESIDENTE") not in ("03", "01" if resident else "02"):
            continue
        end = n(o, "ValiditaOfferta/DATA_FINE") or ""
        if end and datetime.strptime(end[:10], "%d/%m/%Y").date() < today:
            continue
        lo, hi = n(o, "CaratteristicheOfferta/CONSUMO_MIN"), n(o, "CaratteristicheOfferta/CONSUMO_MAX")
        if (lo and kwh_year < float(lo)) or (hi and float(hi) > 0 and kwh_year > float(hi)):
            continue
        if not in_zone(o, region, comune):
            continue
        # ponytail: le offerte riservate (dipendenti, convenzioni, solo con i pannelli…) lo dicono solo nel testo;
        # parole chiave, qualcuna può sfuggire. Il messaggio dice comunque di controllare le condizioni sul sito.
        if RESERVED.search("%s %s" % (n(o, "DettaglioOfferta/NOME_OFFERTA"), n(o, "DettaglioOfferta/DESCRIZIONE"))):
            continue
        if any(n(c, "LIMITANTE") == "01" for c in o.findall(PO_NS + "CondizioniContrattuali")):
            continue
        tf = n(o, "TipoPrezzo/TIPOLOGIA_FASCE")
        fmap = {"01": {"01": BANDS}, "03": {"01": ("F1",), "02": ("F2",), "03": ("F3",)},
                "91": {"01": ("F1",), "91": ("F2", "F3")}}.get(tf)
        var = n(o, "DettaglioOfferta/TIPO_OFFERTA") == "02"
        if fmap is None or (var and n(o, "RiferimentiPrezzoEnergia/IDX_PREZZO_ENERGIA") not in ("01", "08", "12")):
            continue          # ponytail: fasce 07 e indici diversi dal PUN (pochi) saltati, servono le specifiche
        of = dict.fromkeys(BANDS, 0.0)
        of.update(vol=0.0, fix=0.0, pot=0.0, var=var, name=n(o, "DettaglioOfferta/NOME_OFFERTA"),
                  url=n(o, "DettaglioOfferta/Contatti/URL_OFFERTA") or n(o, "DettaglioOfferta/Contatti/URL_SITO_VENDITORE"),
                  code=n(o, "IdentificativiOfferta/COD_OFFERTA"))
        ok = True
        for ci in o.findall(PO_NS + "ComponenteImpresa"):
            macro = n(ci, "MACROAREA")
            for ip in ci.findall(PO_NS + "IntervalloPrezzi"):
                um, p, fc = n(ip, "UNITA_MISURA"), float(n(ip, "PREZZO")), n(ip, "FASCIA_COMPONENTE")
                lo_, hi_ = n(ip, "CONSUMO_DA"), n(ip, "CONSUMO_A")
                if (lo_ and kwh_year < float(lo_)) or (hi_ and float(hi_) > 0 and kwh_year > float(hi_)):
                    continue
                if macro == "04" and um == "03":                 # quota energia (anche spread), per fascia
                    for b in (fmap.get(fc, ()) if fc else BANDS):
                        of[b] += p
                elif macro in ("02", "06") and um == "03":       # commercializzazione e FER al kWh
                    of["vol"] += p
                elif macro in ("01", "06") and um == "01":       # quote fisse €/anno
                    of["fix"] += p
                elif macro == "04" and um == "02":               # quota potenza €/kW/anno
                    of["pot"] += p
                elif macro != "05":                              # una tantum: fuori dall'annuale
                    ok = False                                   # unità ambigua (05 = €/mese o €/anno?): scarto
        if ok:
            out.append(of)
    return out


def pun_avg(csv_text, months=12):
    """Media del PUN (€/kWh) degli ultimi mesi dal CSV storico del Portale Offerte (AnnoMese;PUN;...)."""
    rows = [l.split(";") for l in csv_text.splitlines()[1:] if l.strip()]
    vals = [float(r[1].replace(",", ".")) for r in sorted(rows)[-months:]]
    return sum(vals) / len(vals)


def offer_cost(of, cons, kw, pun, lam):
    """€/anno di materia energia (esclusi dispacciamento, rete, oneri e imposte, uguali per tutti)."""
    idx = pun * (1 + lam) if of.get("var") else 0
    return (sum((of[b] + idx) * cons[b] for b in BANDS) + of["vol"] * sum(cons.values()) + of["fix"]
            + of["pot"] * kw)


def bill_offer(bill):
    """La bolletta attuale nella stessa forma di un'offerta."""
    p = bill.get("eur_kwh")
    of = {b: (p or {}).get(b, 0) if isinstance(p, dict) else (p or 0) for b in BANDS}
    if bill.get("pun_spread") is not None:
        of = {b: bill["pun_spread"] for b in BANDS}
    of.update(vol=0.0, fix=bill.get("eur_year") or 0, pot=0.0, var=bill.get("pun_spread") is not None, name="")
    return of


def savings(bill, cons, kw, offers, pun, lam, base_w=None, k=3):
    """Classifica dei risparmi: le offerte migliori della bolletta attuale e quanto costa il carico di base."""
    now = offer_cost(bill_offer(bill), cons, kw, pun, lam)
    ranked = sorted(offers, key=lambda o: offer_cost(o, cons, kw, pun, lam))
    out = {"now": now, "offers": [], "base": None}
    seen = set()
    for o in ranked:
        c = offer_cost(o, cons, kw, pun, lam)
        if now - c <= 0 or len(out["offers"]) == k:
            break
        if o["name"] not in seen:        # la stessa offerta in più varianti: basta la migliore
            seen.add(o["name"])
            out["offers"].append(dict(o, cost=c, save=now - c))
    if base_w:
        out["base"] = base_w * 8.76 * now / sum(cons.values())    # W × 8760 h / 1000, al prezzo medio di oggi
    return out


def savings_msg(r, cons):
    tot = sum(cons.values())
    lines = [T("💶 Materia energia: oggi ~%d €/anno per %d kWh (F1 %d%%, F2 %d%%, F3 %d%%).") % (
        round(r["now"]), round(tot), *(round(100 * cons[b] / tot) for b in BANDS))]
    for i, o in enumerate(r["offers"], 1):
        lines.append((T("%d. %s: −%d €/anno%s · %s") % (i, o["name"], round(o["save"]),
                                                        T(" (prezzo PUN, stima)") if o["var"] else "", o["url"] or "")).rstrip(" ·"))
    if not r["offers"]:
        lines.append(T("Nessuna offerta del Portale Offerte costa meno della tua: resta così."))
    if r["base"]:
        lines.append(T("Il carico di base (%d W, sempre acceso) costa ~%d €/anno: ogni 10 W in meno sono ~%d €.") % (
            round(r["base_w"]), round(r["base"]), round(r["base"] * 10 / r["base_w"])))
    lines.append(T("Solo materia energia, dal Portale Offerte ARERA di oggi: controlla le condizioni sul sito prima di cambiare."))
    return "\n".join(lines)


def _po_get(path, timeout=120):
    req = urllib.request.Request(PO_URL + path, headers={"User-Agent": "Wattson"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def offers_check(cfg, base_w=None):
    """Scarica le offerte di oggi e il PUN, confronta con la bolletta. Ritorna (risultato, consumi per fascia)."""
    bill = cfg["bill"]
    files = sorted(f for f in glob.glob(os.path.join(DATA, "samples-*.csv")))[-30:]
    samples = []
    for f in files:
        with open(f) as fh:
            samples += [(int(a), int(b)) for a, b in (l.strip().split(",") for l in fh if l.strip())]
    cons = band_split(bill.get("kwh_year"), band_kwh(samples), len(files))
    if not cons:
        raise RuntimeError(T("serve kwh_year in bill (kWh dell'ultimo anno in bolletta) o una settimana di misure"))
    page = _po_get("/portaleOfferte/it/open-data.page").decode("utf-8", "replace")
    xml = re.search(r'href="([^"]*PO_Offerte_E_MLIBERO_\d+\.xml)"', page).group(1)
    par = re.search(r'href="([^"]*PO_Parametri_Mercato_Libero_E_\d+\.csv)"', page).group(1)
    lam = next((float(l.split(",")[1]) for l in _po_get(par).decode("utf-8", "replace").splitlines()
                if l.startswith("lambda,")), 0.1)
    pun = None
    for href in re.findall(r'href="(/portaleOfferte/resources/cms/documents/[^"]+\.csv)"', page):
        txt = _po_get(href).decode("latin-1")
        if "PUN" in txt.splitlines()[0]:
            pun = pun_avg(txt)
            break
    offers = parse_offers(_po_get(xml, 300), sum(cons.values()), bill.get("resident", True), None,
                          region_code(bill.get("region")), bill.get("comune") and "%06d" % int(bill["comune"]))
    r = savings(bill, cons, cfg["limit_kw"], offers, pun or 0, lam, base_w)
    r["base_w"] = base_w
    return r, cons


# --- Telegram guidato: bot nuovo da BotFather fino al notify in HA, senza toccare YAML ---------------------

def tg_api(token, method, payload=None):
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method),
                                 data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(out.get("description") or "Telegram: errore")
    return out["result"]


def chat_from_updates(updates):
    """Ultima chat privata che ha scritto al bot (dopo /start): (chat_id, nome) o None."""
    for u in reversed(updates):
        c = (u.get("message") or {}).get("chat") or {}
        if c.get("type") == "private" and "id" in c:
            return c["id"], " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username", "")
    return None


def cfg_set(key, value):
    """Una chiave nel config.json dell'utente, lasciando com'è tutto il resto."""
    with open(CONFIG_FILE) as f:
        c = json.load(f)
    c[key] = value
    with open(CONFIG_FILE, "w") as f:
        json.dump(c, f, indent=4, ensure_ascii=False)


def ha_flow(ha, path, handler, data):
    """Un config flow di HA a un passo: avvia e invia; ritorna la risposta finale."""
    f = ha.http("/api/config/config_entries/%s" % path, {"handler": handler})
    r = ha.http("/api/config/config_entries/%s/%s" % (path, f["flow_id"]), data)
    if r.get("type") not in ("create_entry",):
        raise RuntimeError("HA ha risposto %s: %s" % (r.get("type"), r.get("errors") or r.get("reason")))
    return r


def telegram_setup(cfg):
    ha = HA(cfg, False)
    set_lang(ha_lang(ha, cfg))
    print("1) Su Telegram apri @BotFather, scrivi /newbot, scegli nome e username del bot.\n"
          "   Alla fine BotFather ti dà un token (tipo 123456:ABC-...). Incollalo qui.")
    while True:
        token = input("Token: ").strip()
        try:
            me = tg_api(token, "getMe")
            break
        except Exception as e:
            print("❌ Token non valido (%s). Riprova." % e)
    print("✅ Bot @%s trovato." % me["username"])
    tg_api(token, "deleteWebhook")      # getUpdates non va se il bot ha un webhook
    print("2) Apri https://t.me/%s e premi Avvia (o scrivi /start). Aspetto fino a 5 minuti..." % me["username"])
    chat, end, off = None, time.time() + 300, None
    while chat is None and time.time() < end:
        ups = tg_api(token, "getUpdates", {"timeout": 25, **({"offset": off} if off else {})})
        chat = chat_from_updates(ups)
        off = ups[-1]["update_id"] + 1 if ups else off
    if chat is None:
        sys.exit("Nessun messaggio arrivato al bot: rilancia wattson.py telegram.")
    if off:
        tg_api(token, "getUpdates", {"offset": off, "timeout": 0})   # svuota la coda: HA non rilegge /start
    tg_api(token, "sendMessage", {"chat_id": chat[0], "text": T("👋 Ciao %s, sono Wattson. Ora collego questo bot a "
                                  "Home Assistant.") % chat[1]})
    print("✅ Chat di %s (%s): ti ho scritto un messaggio di prova." % (chat[1], chat[0]))
    entries = ha.http("/api/config/config_entries/entry")
    if any(e["domain"] == "telegram_bot" and e["title"] in (me["username"], me.get("first_name")) for e in entries):
        sys.exit("Questo bot è già in HA: sceglilo come notify_entity in config.json.")
    r = ha_flow(ha, "flow", "telegram_bot", {"platform": "polling", "api_key": token,
                                              "additional_settings": {"api_endpoint": "https://api.telegram.org"}})
    entry = r["result"]["entry_id"]
    ha_flow(ha, "subentries/flow", [entry, "allowed_chat_ids"], {"chat_id": chat[0]})
    print("✅ Bot aggiunto a Home Assistant, chat autorizzata.")
    notify, ws = None, WS(cfg["ha_url"], ha.token)
    for _ in range(10):                  # l'entità notify nasce qualche secondo dopo la chat
        notify = next((e["entity_id"] for e in ws.call(type="config/entity_registry/list")["result"]
                       if e.get("config_entry_id") == entry and e["entity_id"].startswith("notify.")), None)
        if notify:
            break
        time.sleep(2)
    if not notify:
        sys.exit("Non trovo l'entità notify del bot in HA: sceglila tu come notify_entity in config.json.")
    cfg_set("notify_entity", notify)
    ha.http("/api/services/notify/send_message", {"entity_id": notify, "title": "Wattson",
                                                   "message": T("✅ Wattson collegato: gli avvisi arrivano qui.")})
    print("✅ notify_entity = %s salvato in config.json, messaggio di prova inviato da HA.\n"
          "Riavvia Wattson (systemctl --user restart wattson)." % notify)


def gh_save(r, now):
    """Token dal login o dal rinnovo. Con la scadenza attiva (8 h) arriva anche il refresh token (6 mesi)."""
    j = {"token": r["access_token"]}
    if r.get("refresh_token"):
        j.update(refresh=r["refresh_token"], exp=now + r.get("expires_in", 28800), rt_t=now)
    jsave("github.json", j)
    os.chmod(os.path.join(DATA, "github.json"), 0o600)


GH_KEEPALIVE = 30 * 86400   # rinnova almeno ogni 30 giorni: il refresh token (6 mesi) non muore se non si invia niente


def gh_token(cfg, now):
    """Token valido (rinnovato se serve; col device flow il rinnovo non chiede segreti), o None."""
    j = jload("github.json", {})
    if not j.get("refresh") or (j["exp"] - now > 600 and now - j["rt_t"] < GH_KEEPALIVE):
        return j.get("token")
    try:
        r = _gh("https://github.com/login/oauth/access_token", {
            "client_id": cfg["github_client_id"], "grant_type": "refresh_token", "refresh_token": j["refresh"]})
    except Exception:
        return j["token"] if j["exp"] > now else None
    if "access_token" not in r:
        return None          # refresh token scaduto o revocato: serve un nuovo collegamento
    gh_save(r, now)
    return r["access_token"]


def send_outbox(cfg):
    """Invia i contributi in coda (riprova da sola se la rete o GitHub non ci sono). Ritorna un messaggio o None."""
    if not cfg["catalog_share"]:
        return None
    tok = gh_token(cfg, time.time())      # anche senza niente da inviare: tiene vivo il collegamento
    box = jload("outbox.json", [])
    if not box:
        return None
    if not tok:
        return T("🔑 Condivisione del catalogo attiva ma GitHub non è collegato: collegalo da Wattson → Configura "
                 "(o con wattson.py github).")
    sent = []
    for c in box:
        try:
            _gh("https://api.github.com/repos/%s/issues" % cfg["catalog_repo"], c, tok)
            sent.append(c)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return T("🔑 GitHub non accetta più il collegamento di Wattson: esegui di nuovo wattson.py github.")
            break
        except Exception:
            break
    jsave("outbox.json", [c for c in box if c not in sent])
    return T("📤 Inviato al catalogo condiviso: %s.") % ", ".join(c["title"].split(": ", 1)[-1] for c in sent) if sent else None


def fetch_catalog(cfg):
    """Scarica il catalogo condiviso (pubblico, senza account) in catalog-remote.json."""
    url = "https://raw.githubusercontent.com/%s/main/catalog.json" % cfg["catalog_repo"]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wattson"}), timeout=20) as r:
        cat = json.loads(r.read())
    if isinstance(cat, list):
        jsave("catalog-remote.json", cat)


def catalog_merge(catalog, fps, me, day):
    """Aggiunge/aggiorna il contributo di questa casa (me) per le impronte con tipo. Stessa voce =
    stessi tipo/marca/modello/codice/profilo (senza maiuscole) e potenza entro FP_TOL. Rilanciarlo non conta doppio:
    i valori della voce sono la media, pesata sulle volte viste, dei contributi delle case."""
    key = lambda d: tuple((d.get(k) or "").strip().lower() for k in CAT_FIELDS)
    for fp in fps:
        c = fp.get("cat")
        if not c or not c.get("type") or fp.get("min") is None:
            continue
        pw = lambda d: d["w"] or d.get("avg_w", 0)      # stato "solo consumo": si confronta sui W misurati
        e = next((e for e in catalog if key(e) == key(c) and abs(pw(e) - pw(fp)) <= FP_TOL * pw(e)), None)
        if e is None:
            e = {k: c.get(k, "") for k in CAT_FIELDS}
            e["sources"] = {}
            catalog.append(e)
        e["sources"][me] = dict({"w": fp["w"], "min": fp["min"], "n": fp["n"], "date": day},
                                **{k: fp[k] for k in CAT_REAL if fp.get(k)})
        src = e["sources"].values()
        n = sum(x["n"] for x in src)
        e.update(w=round(sum(x["w"] * x["n"] for x in src) / n), min=round(sum(x["min"] * x["n"] for x in src) / n, 1),
                 n=n, homes=len(e["sources"]))
        for k in CAT_REAL:
            got = [(x[k], x["n"]) for x in src if x.get(k)]
            if got:
                e[k] = round(sum(a * b for a, b in got) / sum(b for _, b in got), 3 if k == "kwh" else 1)
    catalog.sort(key=lambda e: key(e))
    return catalog


def catalog_view(catalog):
    """Voci compatte per la dashboard."""
    return [dict({k: e.get(k, "") for k in CAT_FIELDS}, type=type_label(e.get("type")), kw=round(e["w"] / 1000, 2), min=e["min"], homes=e["homes"],
                 kwh=e.get("kwh"), avg_w=e.get("avg_w")) for e in catalog]


# --- prese fisse: ogni presa con misura (tranne la Profiler) è un profiler permanente --------------
# Un ciclo della presa (accensione → RUN_GAP senza consumo) ha durata ed energia: cicli simili = stesso programma
# (Eco, Intensivo, Cotone 40°…). Wattson li divide da solo; l'utente dà solo il nome al programma.
UI_PLUG = {"presa": "input_select.wattson_presa", "prog": "input_select.wattson_programma",
           "psave": "input_button.wattson_salva_presa"}
SHARE_EVERY = 7 * 86400   # i programmi si affinano a ogni ciclo: al catalogo al massimo una volta a settimana
RUN_GAP = 30 * 60         # tanto senza consumo = ciclo finito (le lavastoviglie restano ferme a lungo mentre asciugano)
RUN_MIN_KWH = 0.05        # sotto: una luce o un'accensione breve, non un programma
RUN_MIN_MIN = 5
PROG_TOL = 0.15           # stesso programma se durata entro ±15 % ed energia entro ±20 %
PROG_TOL_KWH = 0.20
# stati fissi misurati dalla presa, nominabili come i programmi: troppo piccoli per un gradino sul generale,
# nel catalogo vanno come "solo consumo" (w = 0, avg_w = W misurati)


def plug_names(subs, profiler):
    return sorted(n for e, (n, _) in subs.items() if e != profiler) or [NO_PLUG]


def of_plug(fp, name):
    """L'impronta viene da quella presa: stesso nome, o "Nome (x kW)" se ha più livelli."""
    return fp["name"] == name or (fp["name"] or "").startswith(name + " (")


def to_verify(fps, plugs_seen):
    """Impronte con un nome ma senza misura vera (solo contatore generale, stato, a mano): da confermare col Profiler
    prima di entrare nel catalogo condiviso."""
    return [f for f in fps if f["name"] and not f.get("cat") and not f.get("profiler")
            and not any(of_plug(f, n) for n in plugs_seen)]


def idle_w(idle, w):
    """Consumo a riposo della presa (stampante in attesa, standby): scende subito, risale piano.
    0 W = spenta o staccata, non è un riposo: se no lo standby dopo la riaccensione sembra un ciclo infinito.
    ponytail: se la presa è spenta già al primo campione il riposo parte da 0 e lo stesso caso resta."""
    if w == 0 and idle:
        return idle
    return w if idle is None or w < idle else idle + (w - idle) * 0.001


def run_feed(run, w, now, idle=None, max_dt=60):
    """Un campione della presa (None = non letta). Ritorna (ciclo in corso o None, ciclo finito o None).
    max_dt: dal vivo un buco di lettura non gonfia l'energia; sullo storico (solo i cambi) va tolto."""
    if run and run["on"]:           # il valore precedente è rimasto fino ad adesso (sullo storico: ore)
        run["last"] = run["t"] + min(now - run["t"], max_dt)
    done = None
    if run and now - run["last"] > RUN_GAP:     # finito; questo campione può già aprire il ciclo dopo
        done = {"t": run["t0"], "min": round((run["last"] - run["t0"]) / 60, 1), "kwh": round(run["wh"] / 1000, 3),
                "peak": round(run["peak"])}
        run, done = None, done if done["kwh"] >= RUN_MIN_KWH and done["min"] >= RUN_MIN_MIN else None
    if w is None:
        return run, done
    if run:
        run["wh"] += run["w"] * min(now - run["t"], max_dt) / 3600
        run["t"], run["w"] = now, w
    on = idle is not None and w >= idle * 1.3 + PLUG_ON_W   # senza riposo noto il primo campione serve a misurarlo
    if on and run is None:
        run = {"t0": now, "t": now, "w": w, "last": now, "wh": 0.0, "peak": w}
    if run:
        run["on"] = on
    if on:
        run["last"], run["peak"] = now, max(run["peak"], w)
    return run, done


def replay_runs(points, end):
    """Cicli finiti in uno storico della presa [(t, W)] (solo i cambi di valore)."""
    run, idle, out = None, None, []
    for t, w in points + [(end, None)]:
        run, done = run_feed(run, w, t, idle, max_dt=float("inf"))
        if w is not None and run is None:
            idle = idle_w(idle, w)
        out += [done] if done else []
    return out


def plug_levels(st):
    """W misurati a riposo e da acceso per ogni presa fissa: {presa: {"IDLE": W, "ON": W}}."""
    idle, on = st.get("idle", {}), st.get("on_w", {})
    return {n: {"IDLE": idle.get(n), "ON": on.get(n)} for n in set(idle) | set(on)}


def prog_learn(progs, done):
    """Mette un ciclo nel suo programma (o in uno nuovo). Ritorna il programma. Un ciclo già imparato (stesso
    istante di partenza: ricostruito dallo storico e poi chiuso anche dal vivo) non si conta due volte."""
    seen = next((p for p in progs if done["t"] in p.get("seen", [])), None)
    if seen:
        return seen
    same = [p for p in progs if not p.get("state") and abs(done["min"] - p["min"]) <= max(3, PROG_TOL * p["min"])
            and abs(done["kwh"] - p["kwh"]) <= max(0.03, PROG_TOL_KWH * p["kwh"])]
    p = min(same, key=lambda p: abs(done["kwh"] - p["kwh"]) / p["kwh"], default=None)
    if p is None:
        p = {"id": "P%d" % (sum(not q.get("state") for q in progs) + 1), "name": None, "min": done["min"], "kwh": done["kwh"],
             "peak": done["peak"], "n": 0, "seen": []}
        progs.append(p)
    n = p["n"]
    for k, r in (("min", 1), ("kwh", 3), ("peak", 0)):
        p[k] = round((p[k] * n + done[k]) / (n + 1), r) if n else done[k]
    p["peak"] = int(p["peak"])
    p["n"], p["seen"] = n + 1, (p["seen"] + [done["t"]])[-10:]
    return p


def dur(minutes):
    h, m = divmod(round(minutes), 60)
    return "%d h %02d" % (h, m) if h else "%d min" % m


def prog_options(progs, levels=None):
    """Programmi (i senza nome in cima) e poi gli stati fissi misurati (a riposo, acceso)."""
    names = {p["id"]: p["name"] for p in progs if p.get("state")}
    return [T("%s · %s · %s · %s kWh · visto %d volt%s") % (
        p["id"], p["name"] or T("senza nome"), dur(p["min"]), it(p["kwh"], 2), p["n"], "a" if p["n"] == 1 else "e")
        for p in sorted((p for p in progs if not p.get("state")), key=lambda p: (p["name"] is not None, -p["n"]))] + \
        [T("%s · %s · %s W misurati") % (k, names.get(k) or STATES[k], it(w, 1))
         for k, w in (levels or {}).items() if k in STATES and w and w >= 0.5] or [NO_PROG]


def prog_pick(opts, ask, plug):
    """La voce del menu Programma da selezionare per il programma nuovo da nominare (ask = {"plug", "id"}), se il
    pannello mostra la sua presa; None se non c'è niente da cambiare."""
    if not ask or ask.get("plug") != plug:
        return None
    return next((o for o in opts if o.split(" ·")[0] == ask["id"]), None)


def run_msg(plug, p, done, price):
    what = T("%s, %s kWh (%s €)") % (dur(done["min"]), it(done["kwh"], 2), it(done["kwh"] * price, 2))
    if p["name"]:
        return T("✅ %s · %s finito: %s.") % (plug, p["name"], what)
    return (T("🆕 %s ha finito: %s, picco %s kW. %s. Che programma era? Wattson → Impara → Prese fisse.") % (
        plug, what, it(p["peak"] / 1000, 1),
        T("Programma nuovo (%s)") % p["id"] if p["n"] == 1 else T("Stesso programma %s già visto %d volte") % (p["id"], p["n"])))


def plug_save(plugs, progs, name, cat, choice):
    """Dati dell'apparecchio sulla presa fissa + nome del programma scelto (= Profilo). Ritorna il messaggio."""
    if name in ("", NO_PLUG, "unknown", "unavailable"):
        return T("❌ Scegli prima la presa.")
    missing = [CAT_LABELS[k] for k in CAT_FIELDS[:4] if not cat.get(k)]
    pid = choice.split(" ·")[0]
    p = next((p for p in progs.get(name, []) if pid == p["id"]), None)
    if p is None and pid in STATES and not missing:
        p = {"id": pid, "name": None, "state": True}
        progs.setdefault(name, []).append(p)
    if p and not cat.get("profile"):
        missing.append(CAT_LABELS["profile"] + T(" (il nome del programma)"))
    if missing:
        return T("❌ %s: compila tutti i campi. Mancano: %s.") % (name, ", ".join(missing))
    plugs[name] = {k: cat[k] for k in CAT_FIELDS[:4]}
    for q in progs.get(name, []):
        q.pop("sent_n", None)       # dati cambiati: si rimanda
    if p:
        p["name"] = cat["profile"]
    return "✅ %s: %s%s." % (name, " · ".join(plugs[name].values()),
                            T(". %s ora si chiama \"%s\"") % (p["id"], p["name"]) if p else
                            T(". I programmi arrivano da soli, uno per tipo di ciclo: poi li chiami qui"))


def prog_entries(plugs, progs, levels=None):
    """I programmi e gli stati fissi con un nome delle prese con i dati, nel formato delle impronte (catalogo e invio)."""
    out = []
    for plug, cat in plugs.items():
        for p in progs.get(plug, []):
            if not p["name"]:
                continue
            e = {"name": "%s · %s" % (plug, p["name"]), "cat": dict(cat, profile=p["name"]), "_p": p}
            if p.get("state"):
                lv = ((levels or {}).get(plug) or {}).get(p["id"])
                # ponytail: uno stato si manda una volta (n = 1); se il consumo cambia davvero, rinominalo per rimandarlo
                if lv and lv >= 0.5:
                    out.append(dict(e, w=0, min=0, n=1, avg_w=round(lv, 1)))
            elif p["peak"] >= STEP_W:
                out.append(dict(e, w=p["peak"], min=p["min"], n=p["n"], kwh=p["kwh"],
                                avg_w=round(p["kwh"] * 60000 / p["min"])))
    return out


def share_due(pool):
    return {f["name"] for f in pool if (f.get("cat") or {}).get("type") and f.get("min") is not None
            and f.get("sent_n") != f["n"]}


def share(fps, plugs, progs, cfg, now, levels=None):
    """Catalogo locale + coda verso quello condiviso per impronte e programmi nuovi o cresciuti. Messaggio o None."""
    ents = prog_entries(plugs, progs, levels)
    for e in ents:
        e["sent_n"] = e["_p"].get("sent_n")
    pool = fps + ents
    names = share_due(pool)
    if not names:
        return None
    catalog_save(catalog_merge(catalog_load(), pool, install_id(), datetime.fromtimestamp(now).date().isoformat()))
    c = contribution(pool, names) if cfg["catalog_share"] else None
    if c:
        jsave("outbox.json", jload("outbox.json", []) + [c])
    for f in pool:
        if f["name"] in names:
            (f.get("_p") or f)["sent_n"] = f["n"]
    jsave("fingerprints.json", fps)
    jsave("programs.json", progs)
    return send_outbox(cfg)


def stepped(plug, t, dw):
    """Nello storico della presa (solo i cambi) c'è un gradino di ~dw attorno a t?"""
    before = [w for ts, w in plug if ts < t - 15][-1:]
    after = [w for ts, w in plug if t - 15 <= ts <= t + 60]
    return bool(before and after) and abs(max(after) - before[0] - dw) <= MATCH_TOL * dw


def plug_cycles(main, plug, floor=None):
    """Cicli del generale che partono insieme a un gradino della presa: who_stepped, ma sullo storico."""
    d, mine, out = Detector(floor), set(), []
    for t, w in main:
        for ev in d.feed(t, w):
            if ev["type"] == "on" and stepped(plug, ev["t"], ev["w"]):
                mine.add(ev["t"])
            if ev["type"] == "cycle" and ev["t"] in mine:
                out.append(ev)
    return out


def ha_history(ha, entities, t0, t1):
    """Storico del recorder di HA: {entity_id: [(t, W)]} (kW convertiti, stati non numerici saltati)."""
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    r = ha.http("/api/history/period/%s?%s" % (urllib.parse.quote(iso(t0)), urllib.parse.urlencode(
        {"filter_entity_id": ",".join(entities), "end_time": iso(t1)})))
    out = {}
    for serie in r:
        if not serie:
            continue
        k = 1000 if serie[0].get("attributes", {}).get("unit_of_measurement") == "kW" else 1
        pts = []
        for s in serie:
            try:
                pts.append((datetime.fromisoformat(s["last_changed"]).timestamp(), float(s["state"]) * k))
            except ValueError:
                pass
        out[serie[0]["entity_id"]] = pts
    return out


def live_since():
    """Primo campione registrato da Wattson: prima di lì le prese si ricostruiscono dallo storico di HA."""
    days = sorted(f for f in os.listdir(DATA) if f.startswith("samples-")) if os.path.isdir(DATA) else []
    for f in days:
        with open(os.path.join(DATA, f)) as fh:
            line = fh.readline()
        if line.strip():
            return int(line.split(",")[0])
    return time.time()


def backfill_plugs(ha, cfg, fps, st, subs, now, progs, days=10):
    """Una volta per presa: ricostruisce dallo storico di HA (fin dove arriva il recorder) le sue impronte, fino
    all'avvio di Wattson, e i suoi programmi, fino a adesso; da lì in poi li impara dal vivo. Messaggio o None."""
    got, pdone = [], st.setdefault("prog_backfilled", [])
    for e, (name, _) in subs.items():
        if e == cfg["profiler"] or e in pdone:
            continue
        runs = replay_runs(ha_history(ha, [e], now - days * 86400, now).get(e, []), now)
        for r in runs:
            prog_learn(progs.setdefault(name, []), r)
        if runs:
            got.append(T("%s %d programm%s da %d cicli") % (name, len(progs[name]), "a" if len(progs[name]) == 1 else "i",
                                                        len(runs)))
        pdone.append(e)
    jsave("programs.json", progs)
    done, t1 = st.setdefault("backfilled", []), live_since()
    floor = st["bases"][-1]["w"] if st.get("bases") else None
    for e, (name, _) in subs.items():
        if e == cfg["profiler"] or e in done:
            continue
        main_e = cfg["consumption_entity"] or cfg["power_entity"]   # col fotovoltaico: il consumo, non il prelievo
        h = ha_history(ha, [main_e, e], t1 - days * 86400, t1)
        cyc = plug_cycles(h.get(main_e, []), h.get(e, []), floor)
        for c in cyc:
            learn(fps, c, name)
        if cyc:
            got.append(T("%s %d cicl%s") % (name, len(cyc), "o" if len(cyc) == 1 else "i"))
        done.append(e)
    jsave("fingerprints.json", fps)
    jsave("state.json", st)
    return T("📚 Ricostruito dallo storico delle prese: %s.") % ", ".join(got) if got else None


# pannello "Impara" in HA: creato da "wattson.py dashboard", letto dal servizio
UI = {"nome": "input_text.wattson_apparecchio", "stato": "input_text.wattson_stato",
      "white": "input_button.wattson_misura_bianco", "mark": "input_button.wattson_segna_impronta",
      "fp": "input_select.wattson_impronta", "name": "input_button.wattson_dai_nome"}
UI_PROF = {"pstart": "input_button.wattson_inizia_profilo", "pstop": "input_button.wattson_fine_profilo",
           # catalogo condiviso (facoltativi): tipo · marca · modello; il profilo è il campo "stato"
           "tipo": "input_select.wattson_tipo", "marca": "input_text.wattson_marca", "modello": "input_text.wattson_modello",
           "codice": "input_text.wattson_codice_prodotto"}
UI_WAIT = {"white": 120, "mark": 60, "name": 0}   # secondi di misura dopo la pressione


# --- dove sta: stanze (aree di HA) e linee (prese dentro una linea, es. EM06P). Privato: mai nel catalogo ---
UI_WHERE = {"cosa": "input_select.wattson_dove_cosa", "stanza": "input_select.wattson_stanza",
            "linea": "input_select.wattson_linea", "salva": "input_button.wattson_salva_dove"}
NO_AREA = "—"


def meter_tree(measured, parents):
    """Misuratori annidati (presa dentro una linea): ognuno conta una volta sola. parents {nome: misuratore a monte}.
    Ritorna (W dei misuratori in cima, righe {nome, w, dentro, resto_w}); resto_w = parte della linea senza presa."""
    up = {n: parents[n] for n in measured if parents.get(n) in measured and parents[n] != n}
    kids = {}
    for n, par in up.items():
        kids.setdefault(par, []).append(n)
    top = sum(w for n, w in measured.items() if n not in up)
    return top, [{"nome": n, "w": round(w), "dentro": up.get(n),
                  "resto_w": round(w - sum(measured[k] for k in kids[n])) if n in kids else None}
                 for n, w in sorted(measured.items(), key=lambda x: -x[1])]


def where_parents(where, ha_parents):
    """Linea a monte di ogni presa: quella scelta in "Dove sta?" vince (anche "sul generale"), se no quella di HA."""
    out = dict(ha_parents)
    out.update({k[5:]: v["line"] for k, v in where.items() if k.startswith("plug:") and "line" in v})
    return out


def est_id(i, e):
    """Le stime ricostruite dallo storico (estimates.json) non hanno un id proprio: vale la posizione."""
    return e.get("id") or "e%d" % (i + 1)


def where_options(fps, plugs_seen, ests=()):
    """Menu "Cosa" di "Dove sta?": prese/linee, impronte (anche stimate) e stime ricostruite dallo storico."""
    out = [T("plug:%s · presa o linea") % n for n in sorted(plugs_seen)]
    out += ["fp:%s · %s" % (f["id"], f["name"] or T("senza nome"))
            for f in sorted(fps, key=lambda f: (f["name"] is None, f["name"] or ""))]
    out += ["est:%s · %s" % (est_id(i, e), e["nome"].split(" — ")[0][:80]) for i, e in enumerate(ests)]
    return out or [NO_WHAT]


def where_get(where, key, name=None):
    """Dove sta una voce; se non è ancora stata collocata, vale quanto detto per il suo nome ("name:<nome>"):
    così un apparecchio ha la stanza anche prima che Wattson ne abbia l'impronta."""
    return (where or {}).get(key) or ((where or {}).get("name:" + name.strip().lower()) if name else None) or {}


def where_now(where, key, sub_area, parents, name=None, guess=None):
    """Stanza e linea attuali di una voce ("plug:x" / "fp:id"), per precompilare i menu (guess: stanza
    indovinata dagli indizi, se non ce n'è una detta)."""
    w = where_get(where, key, name)
    name = key.split(":", 1)[1]
    area = w.get("area") or (sub_area.get(name) if key.startswith("plug:") else guess)
    line = w["line"] if "line" in w else (parents.get(name) if key.startswith("plug:") else None)
    return area or NO_AREA, line or ON_MAIN


def where_save(where, choice, area, line, plugs_seen, parents):
    """Stanza e linea della voce scelta. Una presa non può stare dentro sé stessa, né in un giro chiuso."""
    key = choice.split(" · ")[0]
    if not key.startswith(("plug:", "fp:", "est:")):
        return T("❌ Scegli prima cosa collocare.")
    area = None if area in (NO_AREA, "", None) else area
    line = None if line in (ON_MAIN, "", None) else line
    if line is not None and line not in plugs_seen:
        return T("❌ %s non è tra le prese o linee con misura.") % line
    name = key.split(":", 1)[1]
    if key.startswith("plug:"):
        up, seen = line, set()
        while up is not None and up not in seen:
            if up == name:
                return T("❌ %s non può stare dentro sé stessa o dentro una sua presa.") % name
            seen.add(up)
            up = parents.get(up)
    where[key] = {"area": area, "line": line}
    return T("✅ %s: stanza %s, %s.") % (name, area or T("non indicata"), "dentro " + line if line else T("direttamente sul generale"))


def ha_context(ws, subs):
    """Da HA: aree, area di ogni presa (dal dispositivo) e linea a monte scelta nella dashboard Energia
    ("dispositivo a monte"): così non si configura due volte. subs: {entity_id: (nome, fattore)}."""
    areas = {a["area_id"]: a["name"] for a in ws.call(type="config/area_registry/list")["result"]}
    devs = {d["id"]: d for d in ws.call(type="config/device_registry/list")["result"]}
    ents = {e["entity_id"]: e for e in ws.call(type="config/entity_registry/list")["result"]}

    def area(e):
        r = ents.get(e) or {}
        return areas.get(r.get("area_id") or (devs.get(r.get("device_id")) or {}).get("area_id"))

    def power_of(stat):
        """Sensore di potenza dello stesso dispositivo del contatore di energia (EM06P: 6 canali, un dispositivo:
        vince il nome più simile)."""
        dev = (ents.get(stat) or {}).get("device_id")
        same = [e for e in subs if dev and (ents.get(e) or {}).get("device_id") == dev]
        best = max(same, key=lambda e: len(os.path.commonprefix([e, stat])), default=None)
        return subs[best][0] if best else None

    prefs = ws.call(type="energy/get_prefs").get("result") or {}
    parents = {}
    for d in prefs.get("device_consumption", []):
        c, p = power_of(d.get("stat_consumption")), power_of(d.get("included_in_stat"))
        if c and p and c != p:
            parents[c] = p
    return (sorted(areas.values()), {n: area(e) for e, (n, _) in subs.items() if area(e)}, parents,
            {e: area(e) for e in ents if area(e)})


def fp_source(fp):
    """Da dove viene un'impronta: determina anche se si possono aggiungere/correggere i dati del catalogo.
    "Profilato con Profiler" e "Misurato con presa" sono misure vere (una presa l'ha vista); "Stimato" è dedotta
    dal solo contatore generale (cicli riconosciuti da soli, o la misura manuale contro un bianco a mano)."""
    return T("Profilato con Profiler") if fp.get("profiler") else "Stimato"


def all_profiles(fps, progs, levels=None, where=None, sub_area=None):
    """Tutti i profili conosciuti (impronte del generale + programmi/stati delle prese fisse), per la vista
    "I miei profili": vista unica di tutto ciò che Wattson ha imparato, generato o ricostruito.
    Uno stato fisso (IDLE/ON) ha uno schema minimo (id/name/state), diverso da un programma a cicli: non ha
    picco/durata/kWh propri, il suo consumo è quello letto ora in levels."""
    out = [{"key": "fp:" + f["id"], "id": f["id"], "nome": f["name"], "fonte": fp_source(f),
            "kw": round(f["w"] / 1000, 2), "min": f["min"], "kwh": f.get("kwh"), "avg_w": f.get("avg_w"),
            "n": f["n"], "cat": f.get("cat"), "modificabile": bool(f.get("profiler") or f.get("cat"))} for f in fps]
    for plug, progl in progs.items():
        for pr in progl:
            key = "prog:%s:%s" % (plug, pr["id"])
            fonte = T("Misurato con presa (%s)") % plug
            if pr.get("state"):
                w = ((levels or {}).get(plug) or {}).get(pr["id"])
                out.append({"key": key, "id": pr["id"], "nome": pr["name"], "fonte": fonte,
                            "kw": None, "min": None, "kwh": None, "avg_w": round(w, 1) if w is not None else None,
                            "n": None, "cat": None, "modificabile": True})
            else:
                out.append({"key": key, "id": pr["id"], "nome": pr["name"], "fonte": fonte,
                            "kw": round(pr["peak"] / 1000, 2), "min": pr["min"], "kwh": pr.get("kwh"), "avg_w": None,
                            "n": pr["n"], "cat": None, "modificabile": True})
    byid = {f["id"]: f for f in fps}
    for e in out:      # stanza da "Dove sta?"; per le prese fisse, se no quella del dispositivo in HA
        plug = e["key"].split(":")[1] if e["key"].startswith("prog:") else None
        e["stanza"] = where_get(where, "plug:" + plug if plug else e["key"], None if plug else e["nome"]).get("area") or \
            ((sub_area or {}).get(plug) if plug else None)
        if not e["stanza"] and not plug:     # indovinata dagli indizi, da confermare
            g = room_guess(byid.get(e["id"], {}))
            e["stanza"] = g + "?" if g else None
    return sorted(out, key=lambda e: (e["nome"] is None, e["nome"] or ""))


def profile_options(fps, progs, levels=None):
    """Menu unico di tutti i profili (impronte + programmi/stati delle prese fisse), per sceglierne uno da
    modificare in "I miei profili". Il prefisso (fp:… o prog:presa:…) è la chiave che profile_edit interpreta."""
    order = all_profiles(fps, progs, levels)   # già ordinati: senza nome in cima
    out = []
    for e in order:
        num = "+%s kW" % it(e["kw"], 1) if e["kw"] else (T("%s W misurati") % it(e["avg_w"], 1) if e["avg_w"] else "—")
        dur = T(" per %d min") % round(e["min"]) if e["min"] else ""
        visto = T(" · vista %d volte") % e["n"] if e["n"] is not None else ""
        out.append("%s · %s · %s · %s%s%s" % (e["key"], e["nome"] or T("senza nome"), e["fonte"], num, dur, visto))
    return out or [NO_FP]


def profile_edit(fps, progs, plugs, choice, name, cat):
    """Modifica un profilo scelto in "I miei profili": un'impronta (fp:…) o un programma/stato di una presa
    fissa (prog:presa:id, delega a plug_save: il nome è il campo Profilo, i dati del catalogo valgono per
    tutti i programmi di quella presa)."""
    if choice.startswith("prog:"):
        _, plug, pid = choice.split(":", 2)
        return plug_save(plugs, progs, plug, cat, pid)
    return fp_edit(fps, choice, name, cat)


def fp_edit(fps, choice, name, cat):
    """Modifica un'impronta esistente dal pannello "I miei profili": nome e/o dati del catalogo.
    I dati del catalogo si possono aggiungere solo a un'impronta già misurata da una presa (Profiler o fissa):
    su un'impronta vista solo dal generale sarebbero una stima, non una misura, e il catalogo condiviso
    vale solo quanto i dati che ci entrano."""
    fid = choice.split(" ·")[0]
    if fid.startswith("fp:"):
        fid = fid[3:]     # scelto dal menu unico di "I miei profili" (chiave "fp:<id>")
    fp = next((f for f in fps if fid == f["id"]), None)
    if fp is None:
        return T("❌ Scegli prima un'impronta dal menu.")
    changed = []
    if name and name != fp["name"]:
        fp["name"] = name
        changed.append(T("nome \"%s\"") % name)
    filled = [k for k in CAT_FIELDS if cat.get(k)]
    if filled:
        if not (fp.get("profiler") or fp.get("cat")):
            return (T("❌ %s: i dati del catalogo si aggiungono solo a un'impronta misurata da una presa. "
                     "Misurala con la presa Profiler.") % fp["id"])
        missing = [CAT_LABELS[k] for k in CAT_FIELDS if not cat.get(k)]
        if missing:
            return T("❌ %s: compila tutti i campi del catalogo. Mancano: %s.") % (fp["id"], ", ".join(missing))
        fp["cat"] = cat
        fp.pop("sent_n", None)   # dati cambiati: si rimanda al catalogo condiviso
        changed.append(T("dati del catalogo"))
    if not changed:
        return T("❌ Scrivi il nome o i dati del catalogo da cambiare.")
    jsave("fingerprints.json", fps)
    return T("✅ %s: aggiornati %s.") % (fp["id"], " e ".join(changed))
UI_GIVEUP = 600                         # poi si rinuncia (consumo mai stabile)


def ui_job(job, fps, now, progs=None, plugs=None):
    """Pressione di un pulsante in attesa → messaggio finale, o None se deve ancora aspettare."""
    if job["kind"] == "name":
        return profile_edit(fps, progs or {}, plugs or {}, job["fp"], job["dev"], job.get("cat", {}))
    if job["kind"] == "mark" and not job["dev"]:
        return T("❌ Scrivi prima il nome dell'apparecchio.")
    if now - job["t"] < UI_WAIT[job["kind"]]:
        return None
    if job["kind"] == "white":
        ok, msg = learn_white(now)
    else:
        ok, msg = learn_mark(fps, job["dev"], job["st"] or "on", now)
    if ok or now - job["t"] > UI_GIVEUP:
        return ("✅ " if ok else "❌ ") + msg
    return None     # non ancora stabile: riprova al prossimo campione


def learn_cli(a):
    now = time.time()
    if a[:1] == ["white"]:
        ok, msg = learn_white(now)
    elif a[:1] == ["mark"] and len(a) >= 3:
        ok, msg = learn_mark(jload("fingerprints.json", []), a[1], " ".join(a[2:]), now)
    elif a[:1] == ["auto"]:
        return learn_auto(now)
    else:
        return print(json.dumps({"bianco": jload("learn.json", {}), "profili": jload("profiles.json", {})},
                                indent=1, ensure_ascii=False))
    if not ok:
        sys.exit(msg)
    print(msg)


def learn_auto(now):
    lj, prof = jload("learn.json", {}), jload("profiles.json", {})
    if "white" not in lj:
        sys.exit("Prima misura il bianco: learn white")
    ch = []
    with open(os.path.join(DATA, "events.jsonl")) as f:
        for l in f:
            e = json.loads(l)
            if e["type"] == "state" and e["t"] >= lj["t"]:
                ch.append((e["t"], e["dev"], e["to"]))
    samples = read_samples(lj["t"])
    fps = jload("fingerprints.json", [])
    for dev, st, t0, t1 in segments(ch, lj["t"], now):
        if t1 - t0 < 90:
            continue
        w = steady(samples, t1 - 5, min(t1 - t0 - 30, 300), max_spread=150)
        if w is not None:
            prof.setdefault(dev, {})[st] = round(w - lj["white"])
            msg = "%s / %s: %d W" % (dev, st, w - lj["white"])
            if w - lj["white"] >= STEP_W:
                fp = seed_fp(fps, "%s (%s)" % (dev, st), w - lj["white"])
                msg += T(" → impronta %s") % fp["id"]
            print(msg)
    jsave("profiles.json", prof)
    jsave("fingerprints.json", fps)


if __name__ == "__main__":
    main(sys.argv[1:])
