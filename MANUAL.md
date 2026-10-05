<p align="center"><img src="logo/wattson-logo.png" alt="Wattson" width="240"></p>

# Wattson — user manual

*Step by step, no electrical or programming knowledge needed.*

> **Language.** Wattson speaks the language of your Home Assistant (Settings → System → General): **Italian** or **English** for now, English for any other language. Alerts, Telegram questions and the dashboard all follow it. To force one, set `language` in `config.json` (`"it"` or `"en"`), then run `python3 wattson.py dashboard --force` to rebuild the dashboard in that language. This manual quotes the Italian names, with the English meaning next to them. With English, you see the English one directly.

---

## 1. What Wattson does, in one minute

Your home has one main electricity meter. When too many things run at once, the meter trips. Home Assistant can tell you **how much** power you use. Wattson tells you **what** is using it and **what to do**:

- 🟢🟡🔴 **A traffic light**: green = go ahead, yellow = wait before starting something big, red = switch something off now.
- 🔍 **It recognises appliances** from their "fingerprint" on the main meter (e.g. *+1.8 kW for about 7 minutes = dishwasher*), even without a plug on them.
- 🌙 **It watches the night-time base load** and tells you if something was left on, and which plug it was.
- 📊 **Every Sunday evening, a weekly summary**: kWh and €, the base load, what it recognised and the heaviest unknown loads.
- 🧺 **With a smart plug it learns every program** of your washing machine, dishwasher or 3D printer, and tells you when each one finishes, how long it took and what it cost.
- 🌍 **It shares what it learns** (optional, anonymous) so every Wattson recognises appliances faster.

Wattson works **only on your local network**. No cloud, no account, unless you choose to share fingerprints.

---

## 2. What you need

| You need | Why | Example |
|---|---|---|
| Home Assistant | Wattson reads your sensors from it | any recent version |
| A sensor on the **main meter** | the only thing Wattson strictly needs | Shelly EM / Pro 3EM, Refoss EM, an ESPHome clamp |
| *(optional)* one **roaming smart plug with power metering** | to teach Wattson appliances without switching the house off | Shelly Plug M, Shelly Plug S |
| *(optional)* **fixed smart plugs** on big appliances | to learn their programs automatically | a plug on the dishwasher |
| *(optional)* a notification service in HA | to get alerts on your phone | Telegram, HA mobile app |

---

## 3. Installation (one time)

Two ways. **A** is the easy one: Wattson runs inside Home Assistant. **B** is the old way, on a separate Linux computer.

### 3A. Inside Home Assistant (recommended)

1. **Copy the integration**: from HACS (*Integrations → ⋮ → Custom repositories →* `https://github.com/tlabcustomaudio/wattson-ha`, category *Integration* → **Download**), or by hand: copy the folder `custom_components/wattson` into HA's `/config/custom_components/`.
2. **Restart Home Assistant.**
3. **Settings → Devices & services → Add integration → Wattson.** It asks four things:

   | Question | What to put |
   |---|---|
   | Main power meter | already filled with your biggest power sensor: check it is the one that measures the whole house |
   | Contract power | 3, 4.5, 6 kW, or type yours (it's on your bill). Wattson takes the trip point as 10 % more |
   | Price per kWh | total of the bill ÷ kWh on the bill |
   | Notifications | where Wattson writes to you (Telegram, the mobile app…). Empty = no messages |
   | Share with the public catalog | optional. On = the appliances you measure with a plug go to the [shared catalog](#8-the-shared-catalog). HA then shows a code: open the link, sign in to GitHub (free account), type the code, come back and press **Submit** |

   **Notifications, no terminal:** the HA **mobile app** works straight away (`notify.mobile_app_…`). For Telegram, add HA's own **Telegram bot** integration (Settings → Add integration → Telegram bot), then pick its `notify.…` here: Wattson's questions then come with buttons you can tap.

✅ **Done.** Within a minute a **Wattson** entry appears in the HA sidebar, with all its buttons and fields. No token, no terminal: Wattson makes its own system user in HA (it disappears if you remove the integration).

- **Change the answers later**: Settings → Devices & services → Wattson → **Configure**.
- **Advanced settings** (`watch`, `base_skip`, `bill`, `language`, `tolerance_kw`…, see [section 10](#10-settings-reference)): write them in `/config/wattson/config.json` (e.g. with the *File editor* add-on), then reload the integration. That file wins over the four questions.
- Wattson's data lives in `/config/wattson/`, so it's in HA's backups.
- **Moving from version B**: stop the old service (`systemctl --user disable --now wattson`), copy `~/.local/share/wattson/` to `/config/wattson/` and your `config.json` next to it, then add the integration.

### 3B. On a separate Linux computer

1. **Get the code**
   ```bash
   git clone https://github.com/tlabcustomaudio/wattson-ha.git ~/wattson
   ```
2. **Create a Home Assistant token**: in HA click your **name** (bottom left) → **Security** → **Long-lived access tokens** → **Create token**. Copy it and save it in a file:
   ```bash
   mkdir -p ~/.config/wattson
   nano ~/.config/wattson/token        # paste the token, save
   ```
3. **Write the configuration**
   ```bash
   cp ~/wattson/config.example.json ~/.config/wattson/config.json
   nano ~/.config/wattson/config.json
   ```
   Change only these lines:

   | Setting | Put here | How to find it |
   |---|---|---|
   | `ha_url` | the address of HA | the one you open in the browser, e.g. `http://192.168.1.50:8123` |
   | `power_entity` | the main-meter power sensor | HA → Settings → Devices → your meter → the sensor in **W** |
   | `limit_kw` | your contract power | on your electricity bill (e.g. 3 kW) |
   | `tolerance_kw` | the power at which the meter actually trips | usually 10 % above the contract (3 kW → 3.3) |
   | `notify_entity` | where alerts go | Developer tools → Actions → search `notify.` (e.g. `notify.mobile_app_phone`) |
   | `price_eur_kwh` | what 1 kWh costs you, all included | total of the bill ÷ kWh on the bill |

   Leave everything else as it is. `watch` and `base_skip` are explained in [section 9](#10-settings-reference).

   **No Telegram bot yet?** Wattson can set one up for you, with buttons to answer its questions. Run `python3 wattson.py telegram` and follow the prompts:
   1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username. Paste the token it gives you.
   2. Open your new bot and press **Start**. Wattson catches your chat and sends you a test message.
   3. Wattson adds the bot to Home Assistant, allows only your chat, writes the `notify_entity` into `config.json` and sends a second test, this time through HA.

   If you already have the Telegram bot in HA, skip this: just put its `notify.…` entity in `notify_entity`.
4. **Check that it works**
   ```bash
   cd ~/wattson
   python3 wattson.py --dry-run       # reads HA without writing; stop it with Ctrl+C
   ```
5. **Create the dashboard** (it also creates all buttons and fields Wattson needs):
   ```bash
   python3 wattson.py dashboard
   ```
   A **Wattson** entry appears in the HA sidebar.
6. **Start it for good** (it restarts by itself after a reboot):
   ```bash
   mkdir -p ~/.config/systemd/user
   cat > ~/.config/systemd/user/wattson.service <<EOF
   [Unit]
   Description=Wattson
   After=network-online.target
   [Service]
   ExecStart=/usr/bin/python3 -u $HOME/wattson/wattson.py
   Restart=always
   RestartSec=30
   [Install]
   WantedBy=default.target
   EOF
   systemctl --user enable --now wattson
   loginctl enable-linger $USER        # keeps it running when you log out (asks for your password)
   ```

✅ **Done.** From now on everything happens in Home Assistant.

**Updating later:**
```bash
cd ~/wattson && git pull && python3 wattson.py dashboard && systemctl --user restart wattson
```

---

## 4. The dashboard: what you see

The **Wattson** dashboard has three tabs.

### Adesso (*Now*)
- **The traffic light** at the top, with advice in plain words. Solar homes also get the **sun advice** under it ([10](#solar-panels)).
- **A gauge** with the current power against your limit.
- **Dove va la corrente** (*Where the power goes*): every metering plug and its watts, plus **Non misurato** (*not measured*): everything else.
- **Dispositivi osservati** (*Watched devices*): state of the devices you listed in `watch` (e.g. the printer is *printing*).
- **Ultime 24 ore** (*Last 24 hours*): the graph.
- **Cosa ha imparato** (*What it learned*): night base load and fingerprints.

### Impara (*Learn*)
Where you teach Wattson. See [section 6](#6-teaching-wattson-five-ways-easiest-first).

### Catalogo (*Catalog*)
The shared list of appliances, grouped by type, plus the fingerprints still **to verify**. See [section 7](#8-the-shared-catalog).

---

## 5. Everyday use: the traffic light and the alerts

| Light | Meaning | What you do |
|---|---|---|
| 🟢 **Via libera** (*Go ahead*) | there is room | nothing |
| 🟡 **Aspetta** (*Wait*) | less than 1.5 kW left before the limit | don't start oven, kettle or hair dryer now. If Wattson knows what is running, it says *"should free up in about N min"* |
| 🔴 **Spegni qualcosa** (*Switch something off*) | you are over the limit, the meter may trip | switch off one of the loads listed in the alert |

**Alerts you can receive** (only when something changes, never repeated):

| Alert starts with | What it means | What you do |
|---|---|---|
| ⚡ *x kW alle hh:mm* | red light: the list says what is on and how much | switch off one of them |
| *Rientrato* (*Back to normal*) | the light is no longer red | nothing |
| 📊 *La settimana di Wattson* (*Wattson's week*) | Sunday 19:00: the week's kWh and €, the base load (how much of it the plugs measure), the recognised appliances and the heaviest unnamed fingerprints | read it; Wattson asks the name of the 3 heaviest unknown ones right after |
| 🔎 *Impronta nuova* (*New fingerprint*) | after the weekly summary: one of the 3 unknown loads that used the most energy that week (at least 0.5 kWh) | give it a name, or tap *Non so*: [6.4](#64-name-a-fingerprint-wattson-found) |
| *Ho riconosciuto un carico* (*I recognised a load*) | a fingerprint got its name by itself | nothing |
| *Carico di base stanotte* (*Base load tonight*) | the house used at least 60 W more than usual at night | look for something left on. The alert shows the yearly cost and, if a measured plug went up at night, which one (*Di cui: +18 W Stampante*) |
| ✅ *… finito* (*… finished*) | a program on a fixed plug ended: duration, kWh, € | nothing |
| 🆕 *… ha finito* (*… has finished*) | a fixed plug finished a program Wattson has no name for | name the program: [6.3](#63-fixed-plugs-programs-learned-automatically) |
| 🔌 *La presa … sembra stare sulla linea …* | a plug and a line rose together 3 times | tap Sì or No ([6.6](#66-where-it-is-rooms-and-lines)) |
| 🔑 *GitHub* | catalog sharing is on but the GitHub link expired | Wattson → **Configure** → **Submit**: with sharing on, it shows a new code to type (or run `python3 wattson.py github`) |
| ☀️ *Usa il tuo sole* (*Use your sun*) | solar homes: you have been exporting enough for 10 min with the battery full. It lists the known heavy loads that fit | start them now |
| 🌥️ *Finito il surplus* (*Surplus over*) | the sun window above closed | heavy loads that can wait, wait for the next one |

---

## 6. Teaching Wattson: six ways, easiest first

You don't have to teach Wattson anything: it learns by itself. Teaching makes it **faster and more precise**.

> 💬 **Answer from Telegram.** If your Home Assistant has the **Telegram bot** integration (not only a notify service), Wattson's questions arrive with buttons: *"🔎 New fingerprint: +0.3 kW for 142 min, seen 5 times. What is it?"* (asked only after the Sunday summary, for the 3 unknown loads that used the most energy; a question left unanswered is dropped there and asked again only if that load is still among the heaviest), *"📍 This seems to be in the laundry room: right?"*. Tap a button, or **just write the answer** to the bot (a name, or a room — even part of it, like *lavand*): it answers the latest open question and you get a confirmation right away. Only the chats you allowed in the Telegram integration can answer. Without the bot, the same questions arrive as plain alerts and you answer in the dashboard.

### 6.1 Do nothing (automatic)
- Every load ≥ 0.15 kW that switches on and off becomes a **fingerprint**.
- It gets a name by itself if, at the same moment, a metering plug in HA saw the same jump, or a watched device changed state.
- A fingerprint named after a metering plug only matches when that plug jumps too. Example: the oven and the dishwasher both pull ~2 kW, but if the dishwasher plug stays at 0 W, the jump can't be the dishwasher.
- New metering plugs you add to HA are found **within an hour**.

### 6.2 The Profiler plug (recommended)

A smart plug you move from one appliance to another. **You don't switch anything off**: Wattson compares the plug with the main meter.

**First time only:**
1. Plug a smart plug **with power metering** into the wall.
2. Add it to Home Assistant, the way that plug normally is added (Shelly: HA finds it by itself).
3. **Rename it `Profiler`**: Settings → Devices & services → Devices → click the plug → pencil ✏️ at the top → name `Profiler` → confirm, and accept renaming the entities.
4. That's all. Wattson finds a plug called *Profiler* by itself. If you press the start button before it did, it looks again straight away.

**Every appliance:** go to **Impara** → *Impara con la presa Profiler* (*Learn with the Profiler plug*).

1. Fill in **all** fields. They are all required: the shared catalog is only as good as the data in it.

   | Field | English | Example |
   |---|---|---|
   | **Nome device** | what you call it at home. **Never shared**: it stays in your home | *Bathroom dehumidifier* |
   | **Tipo** | type of appliance, **picked from a list** (the same list in every Wattson, so the catalog groups them right) | *Deumidificatore* (*Dehumidifier*) |
   | **Marca** | brand | *Acme* |
   | **Modello** | model name | *AirDry 12* |
   | **Codice prodotto** | product code, from the label on the back or the manual | *AD-1200* |
   | **Profilo** | the mode you are about to run | *Laundry Dry* |

2. Press **Inizia profilo** (*Start profile*).
3. Plug the appliance into the Profiler plug (better if still off), then switch it on.
4. Let it run a **full cycle**, even hours.
5. Press **Fine profilo** (*Stop profile*), or simply **unplug the Profiler**: after 2 minutes Wattson closes by itself. It also closes after 8 hours if you forget.

The result appears on the dashboard and as an alert: duration, kWh, peak and the fingerprints found. From then on Wattson recognises that appliance **even without the plug**.

> 💡 Same appliance, different modes? Do one session per mode, changing only **Profilo** (e.g. *ABS print*, then *PLA print*).
> 💡 Below 0.15 kW there's no step to spot on the main meter, so Wattson can't recognise the appliance without the plug — but the measured profile still lands in "I miei profili" and the shared catalog (as "measured, no main-meter step"), same as anything else genuinely measured with a plug.

### 6.3 Fixed plugs: programs learned automatically

A metering plug that **always stays on the same appliance** (dishwasher, washing machine, dryer, 3D printer) is a permanent Profiler.

**What Wattson does by itself:**
- every time the appliance runs (from switch-on until 30 minutes of silence), it measures **duration and kWh**;
- runs that look alike are the **same program**: *Eco* and *Intensive* become **P1** and **P2** without you doing anything;
- at the end of every run you get an alert with duration, kWh and cost;
- the plug's standby is measured, so a printer waiting at 30 W is not a "run".

**What you do, once per plug and once per program:** **Impara** → *Prese fisse* (*Fixed plugs*):

1. **Presa** (*Plug*): pick the plug. The fields fill in with what you saved before.
2. Fill in **Tipo, Marca, Modello, Codice prodotto** (only the first time).
3. **Programma** (*Program*): pick one. The newest one without a name is at the top: it is the one that just finished.
4. **Profilo**: write the program's name as the appliance calls it (*Eco 50°*, *Cotton 40°*, *PLA print*).
5. Press **Salva dati presa** (*Save plug data*).

From then on the alert says *"✅ Dishwasher · Eco 50° finished: 2 h 05, 0.95 kWh (0.16 €)"*.

> 💡 **The easy trick:** when the 🆕 alert arrives, you know what you just ran. Name it right away.
> 💡 A plug that was already running when you connected it: that first run has no real start and is ignored. The next one counts.
> 💡 Plugs on lights or small loads (< 0.05 kWh per run) are ignored for programs.

### 6.4 Name a fingerprint Wattson found
**Impara** → *Dai un nome a un'impronta* (*Name a fingerprint*):
1. **Impronta**: pick it from the menu (the unnamed ones, most seen first, are at the top).
2. **Nome device**: write what it is.
3. Press **Dai il nome** (*Give the name*). It also fixes a wrong name.

> ⚠️ A name you give this way is an **estimate**: no plug measured it. It works for recognising the appliance at home, but it goes into the **to verify** list and not into the shared catalog, until you measure it with the Profiler.

### 6.5 Manual measurement (old way, needs switching things off)
**Impara** → *Insegna a Wattson*:
1. Switch off everything you can, wait a minute, press **Misura bianco** (*Measure the baseline*). It needs 2 minutes of steady consumption.
2. Write the **Nome device**, switch that appliance on, wait until it is steady, press **Segna impronta** (*Mark fingerprint*). One minute later it records how much it uses above the baseline.

Use it only if you have no Profiler plug.

### 6.6 Where it is: rooms and lines
**Impara** → *📍 Dove sta* (*Where it is*). Two things Wattson can't guess from watts alone:

- **Stanza** (*Room*): the rooms are your Home Assistant areas. Plugs take their room from their device in HA by themselves; for an appliance seen only on the main meter (a fingerprint), tell Wattson. When two appliances use the same power (three dehumidifiers at ~200 W look identical on the main meter), the room is how Wattson will tell them apart, using the sensors in that room: humidity dropping, temperature rising, a light or a switch changing at the same moment. Every sensor you add later to an area becomes one more clue, with nothing to configure.
- **Dentro la linea** (*Inside the line*): if a plug sits on a circuit that is itself measured (for example one channel of a multi-line meter like a Refoss EM06P or a Shelly Pro 3EM), pick that line. Wattson then **counts that plug only once**: the line shows its own total, the plug inside it, and *senza presa* (*without a plug*), the part of the line no plug measures. If you already set the **upstream device** in HA's Energy dashboard, Wattson reads it from there and you don't need to do it twice.

  **Wattson also finds the line by itself.** When a plug jumps by 100 W or more, it looks at the other meters in the same seconds: a line that rose by about the same amount gets a point, a line that stayed flat is ruled out for good. After 3 matches with no contradiction you get a question: *🔌 La presa X sembra stare sulla linea Y* (*plug X seems to be on line Y*). Tap **Sì** and it's saved, the same as choosing it here. Tap **No** and Wattson never asks again.

1. **Cosa** (*What*): pick a plug/line, a fingerprint (estimated ones too), or an estimate rebuilt from history (the ones in *Da verificare*). The fields fill in with what Wattson already knows.
2. Fix **Stanza** and **Dentro la linea**.
3. Press **Salva dove sta** (*Save where it is*).

Rooms and lines stay in your home: they are **never** sent to the shared catalog.

**How Wattson uses the rooms (it learns by itself):**
- When a load starts, Wattson notes what changed **in each room in that same minute**: a light or a switch turning on, a climate unit starting, a door opening, motion, a printer changing status.
- When a load of 10 minutes or more ends, it checks whether a room's **humidity or temperature** moved while it ran (humidity down = dehumidifier, temperature up = oven or heater, down = air conditioner), net of how it was already going before. Appliance temperatures above 45 °C (nozzles, beds) don't count as a room.
- Each fingerprint counts its clues over time. When several fingerprints have the same power, Wattson picks the one whose clues match **now**, or whose room you set.
- If a clue from one room keeps coming back (at least 2 runs and 60 % of the observed ones), Wattson guesses the room: you see it with a question mark in **I miei profili** (*Lavanderia?*), it's pre-selected in **📍 Dove sta**, and you get one alert asking you to confirm.

> 💡 The best clue is a switch: if an automation turns the dehumidifier on through a smart switch or plug, the switch changes state in the very minute the load starts. Just make sure the switch is in the right **area** in HA. Every sensor you add to a room later becomes a clue by itself.

> Example: the dishwasher plug is on the *Cucina* line of your line meter. Without telling Wattson, 1.8 kW of washing would count twice (once on the plug, once on the line) and *Non misurato* would go negative. With *Dentro la linea: Linea Cucina*, **Adesso** shows *Linea Cucina 1.9 kW (senza presa: 150 W)* with *Lavastoviglie 1.75 kW* under it.

---

## 7. Getting the most out of Wattson: techniques and examples

### 7.1 The first week, day by day

| Day | Do this | Time |
|---|---|---|
| 1 | Install, open the **Adesso** tab, check that the gauge moves when you switch the kettle on | 20 min |
| 2 | Rename your metering plug **Profiler** and profile **one short appliance** (kettle, hair dryer) to see how it works | 10 min |
| 3 | **Profiler day** (see 7.3): all the small appliances, one after another | 1 hour, spread over the day |
| 4 | Put the Profiler on a **long** appliance (dishwasher, washing machine) for a full cycle | start it and forget it |
| 5 | Name the fingerprints Wattson found by itself (the *Impronta nuova* alerts) | 5 min |
| 6–7 | Read the first **base load** figure in *Cosa ha imparato*. Hunt for waste if it is high (see 7.5) | 15 min |

After the first week Wattson recognises most of what runs in your home, and you only name the new things.

### 7.2 Where to put the plugs you have

Plugs are worth the most where **big loads run often**. Rough priority:

1. **Dishwasher, washing machine, dryer**: long, frequent, several programs. A **fixed plug** here pays back fastest: you get every program learned and a *finished* alert.
2. **3D printers, dehumidifiers, heaters, air conditioners on a socket**: long runs, several modes.
3. **The Profiler**: keep one plug roaming for everything else.
4. **Hard-wired loads** (oven, hob, boiler, heat pump) can't use a plug: a DIN-rail meter or a Shelly PM Mini on their line does the same job. Wattson treats any power sensor like a plug.
5. **A meter per line** (a multi-channel meter in the fuse box): every line becomes a small main meter with much less noise, so small loads become visible and same-power appliances on different lines stop being confused. Plugs on those lines can stay: tell Wattson which line each one is on (**📍 Dove sta**) and nothing is counted twice.

> Example: you have 2 plugs. Put one **fixed on the dishwasher** and keep the other as **Profiler**. After a month the dishwasher has its programs learned, and the Profiler has visited ten appliances.

### 7.3 The Profiler day: many appliances in one go

Short appliances take only a few minutes each. Prepare the list, then for each one: fill the fields → **Inizia profilo** → plug in → use it normally → **Fine profilo**.

| Appliance | How to use it during the session | Session length | You'll see roughly |
|---|---|---|---|
| Kettle | boil a full kettle | 3–4 min | +2.0 kW for 3 min |
| Hair dryer | full heat, then cool | 3 min | +1.3 kW, then lower |
| Microwave | one glass of water for 2 min | 3 min | +1.1–1.4 kW |
| Toaster | one round | 3 min | +0.8 kW |
| Iron | heat up and iron one shirt | 10 min | +1–2 kW pulses |
| Vacuum cleaner | one room | 5 min | +0.6–1.2 kW |
| Coffee machine | one coffee from cold | 5 min | +1.2 kW heating, then short pulses |

> 💡 One mode per session. For the hair dryer, *Profilo* = *Hot* for one session and *Cold* for another, if you care about both.
> 💡 Start each session with the appliance **off** and switch it on only after *Inizia profilo*: Wattson needs to see the jump.

### 7.4 Appliances with several modes or programs

- **Roaming (Profiler):** one session per mode. Same Tipo/Marca/Modello/Codice, different **Profilo**.
  *Example, 3D printer:* session 1 *Profilo* = `PLA print`, session 2 = `ABS print`, session 3 = `Idle`. ABS heats the bed and chamber more: it gets its own fingerprint.
- **Fixed plug:** do nothing. Run the appliance normally for a couple of weeks: each program you use appears as P1, P2, P3… Name each one **when its 🆕 alert arrives**, because at that moment you know which program you just ran.
  *Example, dishwasher:* Monday you run *Eco* → 🆕 alert, P1 → you name it `Eco 50°`. Wednesday *Intensive* → 🆕, P2 → `Intensive 70°`. Friday *Eco* again → *"✅ Dishwasher · Eco 50° finished: 3 h 10, 0.92 kWh (0.15 €)"*.
- **Use the names printed on the appliance** (knob, display, manual). Every Wattson then uses the same names and the shared catalog matches them.

### 7.5 Hunting the night base load

The base load is what the house uses between 2 and 5 am. Every 10 W running all year costs about 88 kWh, roughly 15–25 €.

*Example:* the alert says *"Base load tonight: 210 W, usually 145 W (+65 W ≈ 95 €/year)"*.
1. Think of what changed yesterday: a new device? a PC left on? a heater?
2. If the alert ends with *Di cui: …* (*Of which*), that plug is the culprit. The weekly summary also splits the base load into *dalle prese* (*from the plugs*, each with its watts) and *non misurati* (*not metered*).
3. Check **Dove va la corrente** in the morning: measured plugs show their watts.
4. Suspect something unmeasured? Put the **Profiler** on it for a night with a session named *Standby* (TV, NAS, game console, old fridge).
5. Or do it the quick way: watch the gauge and switch off one suspect at a time. A drop of 60 W on the gauge is your culprit.

> 💡 A printer or dryer running all night spoils the figure. List it in `base_skip` so Wattson ignores those nights.

### 7.6 Use the traffic light before you start something big

*Example:* the dishwasher is heating (+1.8 kW) and someone wants the hair dryer (+1.3 kW). With a 3 kW contract that is 3.1 kW plus the base: the meter can trip.
- The light is 🟡 **Aspetta** with *"should free up in about 4 min"*: Wattson knows the dishwasher heats for ~7 minutes.
- Wait 4 minutes, the light turns 🟢, then use the hair dryer.

Put the gauge card on your main HA dashboard or a wall tablet, so everyone sees it.

### 7.7 Let HA act on the traffic light

`sensor.wattson_semaforo` is `go`, `wait` or `stop`, and its attributes include `margine_kw` (kW left) and `potenza_kw`. Use them in automations.

*Pause a low-priority load on red, resume after 5 minutes of green:*
```yaml
alias: Wattson - pause the car charger on red
triggers:
  - trigger: state
    entity_id: sensor.wattson_semaforo
    to: stop
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.ev_charger
```
```yaml
alias: Wattson - resume the car charger
triggers:
  - trigger: state
    entity_id: sensor.wattson_semaforo
    to: go
    for: "00:05:00"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.ev_charger
```

*Start the dryer only if there is room:*
```yaml
conditions:
  - condition: template
    value_template: "{{ state_attr('sensor.wattson_semaforo', 'margine_kw') | float(0) > 2.5 }}"
```

> ⚠️ Only automate loads that can safely stop and restart (chargers, boilers, dehumidifiers). Never a freezer or medical equipment.

### 7.8 Help Wattson name things by itself with `watch`

Devices that HA knows but that have no plug (printer with its integration, washing machine with a cloud integration, heat pump) can still help: list their **state sensor** in `watch`. When a jump on the main meter happens at the same moment as a state change, twice, the fingerprint takes that name.

```json
"watch": {
  "sensor.printer_print_status": "3D printer",
  "sensor.washer_machine_state": "Washing machine"
}
```

### 7.9 Finding the product code

| Appliance | Where the label is |
|---|---|
| Dishwasher | on the edge of the door, visible when open |
| Washing machine / dryer | inside the door frame, or behind the filter flap at the bottom |
| Dehumidifier, heater, air conditioner | on the back or underneath |
| Fridge | inside, on the side wall near the vegetable drawer |
| 3D printer | back or bottom sticker; the model code is also in the maker's app |
| Small appliances | underneath, next to the CE mark |

It is the code next to *Model*, *Mod.* or *Type* (e.g. `AB123CD45E`, `AD-1200`), **not** the serial number (*S/N*).

---

### 7.10 Your bill against today's offers (Italy)

Wattson can compare what you pay with **every offer on the ARERA Portale Offerte** (the official public list, updated daily), using **your real consumption split by time band** (F1/F2/F3, with Sundays and national holidays, Easter Monday included), measured by Wattson itself.

1. From your bill, add to `config.json`:
   ```json
   "bill": {
       "kwh_year": 2700,
       "eur_kwh": 0.13,
       "eur_year": 96,
       "resident": true,
       "region": "Lombardia",
       "comune": "015146"
   }
   ```
   | Field | Where on the bill |
   |---|---|
   | `kwh_year` | *consumo annuo* (kWh of the last 12 months). Leave it out and Wattson extrapolates it from a week or more of measurements |
   | `eur_kwh` | the energy price, *quota energia* / *prezzo componente energia* (€/kWh, without network charges and taxes). With prices per band: `{"F1": 0.14, "F2": 0.12, "F3": 0.11}`. With a PUN-indexed offer use `"pun_spread": 0.02` (the spread) instead |
   | `eur_year` | the fixed fee of the **sale** part: *quota fissa vendita* / *commercializzazione*, in €/year (12 × the monthly one) |
   | `resident` | `true` if it's your home of residence |
   | `region`, `comune` | to include local offers: region name, and the **ISTAT code** of your town (6 digits, search "codice ISTAT" + your town). Without them, only nationwide offers count |
2. Run `python3 wattson.py offerte`. Example:
   ```
   💶 Materia energia: oggi ~471 €/anno per 2700 kWh (F1 31%, F2 28%, F3 42%).
   1. Luce Flex: −31 €/anno (prezzo PUN, stima) · https://www.illumia.it/...
   2. ...
   Il carico di base (115 W, sempre acceso) costa ~175 €/anno: ogni 10 W in meno sono ~15 €.
   ```
3. With `bill` set, the service also checks once a month and sends you the list **only if** you could save at least 30 €/year.

What it compares: only the **energy component** (*materia energia*: energy price per band, spread, sale fees, as in ARERA's calculation rules). Network charges, system charges, dispatching and taxes are the same whatever supplier you pick, so they are left out. PUN-indexed offers use the average PUN of the last 12 months: an **estimate**, the future PUN is unknown. Offers reserved to some customers (employees, members, conventions, bundles) are left out when the text says so. Some may slip through, so **always read the conditions on the supplier's site** before switching. One-off discounts are not counted.

## 8. The shared catalog

A public list of appliances and their fingerprints: [wattson-catalog](https://github.com/tlabcustomaudio/wattson-catalog), grouped by **Type · Brand · Model · Product code · Profile**.

- **Reading: always, for everyone, no account.** Once a day Wattson downloads it. When it finds an unknown fingerprint, the alert suggests *"looks like: Dehumidifier Acme AirDry 12"*.
- **Sharing: only if you turn it on.**
  1. In `config.json` set `"catalog_share": true`.
  2. Turn on **Share with the public catalog** in Wattson → **Configure** (or run `python3 wattson.py github`): it shows a code, open the link, type the code, authorise **Wattson Catalog**. Like installing HACS.
  3. Restart: `systemctl --user restart wattson`.
- **What is sent:** only type, brand, model, product code, profile, watts, minutes, kWh, times seen. **Never** your device names, rooms, times or address. Your GitHub name is stored only as an anonymous code.
- **Only measured data is shared.** Fingerprints from the Profiler or from a fixed plug can be shared. Those named from the main meter alone are listed in **Catalogo** → *Da verificare con il Profiler* (*To verify with the Profiler*) and stay at home.
- Every entry has **two kinds of numbers**:
  - **Gradino** (*step*): the jump Wattson sees on the main meter. It is how the appliance is **recognised**.
  - **Consumo** (*consumption, W*) and **kWh/ciclo** (*kWh per run*): what the plug **really measured**.

  They can differ. *Example:* a dehumidifier starts its fan first (35 W, too small for the main meter), then the compressor (+170 W). Step = 170 W, real consumption = 210 W, 0.25 kWh in 72 minutes.
- In the **Catalogo** tab, *solo tua* (*only yours*) marks entries not yet in the shared list.

### Where every fingerprint and program comes from

**Catalogo** → **I miei profili**: one table with everything Wattson has learned on your fingerprints and your fixed plugs' programs/states, each tagged by its **Fonte** (*source*):

| Fonte | Means | Can it carry catalog data? |
|---|---|---|
| **Stimato** (*Estimated*) | seen only on the main meter (auto-detected, or a hint from a watched device) | No: measure it with the Profiler first |
| **Profilato con Profiler** (*Profiled with the Profiler*) | learned in a Profiler session | Yes |
| **Misurato con presa (*plug*)** (*Measured by a plug*) | a fixed plug's program or steady state | Yes |

Right next to the table, **Modifica un profilo** (*Edit a profile*): pick anything from the list, its fields fill in, correct them and press **Modifica profilo**. For a fingerprint, you can always fix its name; you can only add or correct catalog data (Type/Brand/Model/Code/Profile) if the *Fonte* says it was really measured. For a fixed plug's program, the name goes in **Profilo**, and Type/Brand/Model/Code apply to every program of that plug. The same select and button also sit in **Impara** → **③ Dai un nome**.

---

## 9. Problems and fixes

| Problem | Fix |
|---|---|
| The dashboard shows old numbers | is Wattson running? `systemctl --user status wattson` must say *active* |
| *"Non trovo la presa Profiler"* (*Can't find the Profiler plug*) | the plug must be in HA with power metering, named exactly `Profiler`. Rename it and press *Inizia profilo* again |
| *Inizia profilo* says fields are missing | all six fields are required, product code included |
| The Profiler session found no fingerprint | the appliance used less than 0.15 kW, or was already on when you started: start the session with the appliance **off** |
| A fixed plug never sends a finished alert | a run ends after **30 minutes** of silence. Loads under 0.05 kWh per run are ignored |
| Two programs got mixed up | runs within ±15 % duration and ±20 % kWh count as one program: that's normal for very similar programs |
| The **Programma** menu says *nessun ciclo ancora* (*no runs yet*) | that plug has not finished a full run yet |
| The plug says 218 W but the catalog says 170 W | both are right. The catalog's **step** (*Gradino*) is the jump seen on the main meter, used to recognise the appliance. **Consumption** (*Consumo*) and **kWh per run** are what the plug really measured. Many appliances start in stages (fan first, compressor later), and the main meter only shows jumps from 0.15 kW up |
| A metering plug doesn't appear | Wattson looks for new sensors every hour. It must be a power sensor in W or kW. Estimated sensors (e.g. powercalc) are ignored on purpose |
| I changed the main-meter sensor | edit `power_entity` in `config.json`, then `systemctl --user restart wattson` |
| 🔑 GitHub alert | Wattson → **Configure** → **Submit**, then type the new code it shows |

To see what Wattson is doing: `journalctl --user -u wattson -f`.

---

## 10. Settings reference

`~/.config/wattson/config.json`. Restart Wattson after every change.

| Setting | Default | Meaning |
|---|---|---|
| `ha_url` | — | Home Assistant address |
| `token_file` | `~/.config/wattson/token` | file with the HA token |
| `power_entity` | — | main-meter power sensor |
| `grid_signed` | false | **Solar homes.** `true` if `power_entity` is the net grid exchange and goes **negative** when you export |
| `export_entity` | none | **Solar homes.** Grid export power, if your meter gives it as a separate sensor (positive when exporting) |
| `pv_entity` | none | **Solar homes.** Solar production power |
| `consumption_entity` | none | **Solar homes.** House consumption power, if your inverter already computes it (best choice with a battery): it wins over the formula |
| `limit_kw` | 3.0 | contract power: red above this |
| `tolerance_kw` | 3.3 | where the meter really trips |
| `big_load_kw` | 1.5 | yellow when less than this is left |
| `notify_entity` | none | where alerts go |
| `price_eur_kwh` | 0.25 | cost of 1 kWh, for € figures |
| `battery_level_entity` | none | **Solar homes.** Battery charge, % |
| `battery_power_entity` | none | **Solar homes.** Battery power, **positive when discharging** (Sungrow) |
| `battery_charge_positive` | false | `true` if your battery power is positive when **charging** instead |
| `battery_full_pct` | 95 | above this the battery counts as full: what you export is true surplus |
| `sun_load_kw` | 2.0 | export needed before Wattson says *use your sun* |
| `sun_min` | 10 | minutes the condition must hold before the advice changes (a passing cloud doesn't count) |
| `feed_in_eur_kwh` | none | what you are paid per exported kWh. With it, the sun alert says how much each kWh saves |
| `watch` | `{}` | devices without metering to watch, `{"sensor.printer_status": "Printer"}`: their state changes help name fingerprints |
| `base_skip` | `{}` | nights not to use for the base load, `{"sensor.printer_status": ["printing"]}`: a printer printing all night would spoil the average |
| `submeters_exclude` | `[]` | power sensors to ignore |
| `profiler` | automatic | the Profiler plug sensor. Leave it out: the plug called *Profiler* is found by itself |
| `catalog_share` | false | send measured fingerprints to the shared catalog |
| `dashboard` | `wattson` | dashboard address in HA |
| `bill` | none | **Italy.** Your bill, to compare it with the ARERA offers: [7.10](#710-your-bill-against-todays-offers-italy) |
| `language` | HA's language | `"it"` or `"en"`: language of alerts and dashboard. Leave it out to follow Home Assistant |

### Solar panels

With solar panels the main meter only shows what you **buy**: when the sun covers the dishwasher, the grid reading stays at zero and Wattson wouldn't see it start. Set the solar settings above and Wattson splits two things:

- **House consumption** (import − export + solar, or your inverter's own consumption sensor): used to recognise appliances, the night base load and the profiles.
- **Grid exchange**: used by the traffic light. The meter only trips on what you import, so what you export is extra room: exporting 1.8 kW with a 3.3 kW limit means 5.1 kW of margin.

Without solar settings nothing changes. Batteries: use `consumption_entity` for appliance recognition.

**Sun advice: when to run heavy loads.** With `grid_signed` or `export_entity` set, Wattson also tells you which energy a heavy load would use right now. The cheapest is the sun you would otherwise export, then the battery, then the grid:

| Advice | When | What you do |
|---|---|---|
| ☀️ *Usa il tuo sole* | export ≥ `sun_load_kw` and battery ≥ `battery_full_pct` | start heavy loads: the alert lists the known ones that fit, biggest first |
| ⏳ *La batteria si sta caricando* | the battery is charging and not full | if it can wait, wait: a load now takes the energy the battery would keep for the evening |
| 🌙 *Adesso va a batteria o rete* | anything else (evening, night, cloudy) | if it can wait, wait for the next sun window |

Only the ☀️ window opening and closing sends an alert (at most 4 a day). The rest is on the dashboard, under the traffic light. "Known heavy loads" are named fingerprints and fixed plugs drawing at least `big_load_kw`.

Example for a Sungrow hybrid inverter (mkaiser Modbus):
```json
"power_entity": "sensor.meter_active_power",
"grid_signed": true,
"battery_level_entity": "sensor.battery_level",
"battery_power_entity": "sensor.battery_power",
"feed_in_eur_kwh": 0.03
```
If HA sees only part of your batteries, `battery_level` describes only that part. Lower `battery_full_pct` if the visible battery fills later than the others.

---

## 11. Command line (for the curious)

```bash
python3 wattson.py fp                         # list fingerprints
python3 wattson.py name fp3 Kettle            # name a fingerprint
python3 wattson.py catalog                    # print your catalog entries
python3 wattson.py github                     # link GitHub for sharing
python3 wattson.py telegram                   # create a Telegram bot and connect it to HA, step by step
python3 wattson.py offerte                    # your bill against today's ARERA offers (Italy)
python3 wattson.py dashboard                  # create/update helpers and dashboard
python3 wattson.py learn white | show         # manual baseline / show what was learned
```

Data lives in `~/.local/share/wattson/`: `fingerprints.json`, `programs.json`, `plugs.json`, `profiles.json`, `catalog-mine.json`, daily `samples-*.csv`.

---

## 12. Words used in this manual

| Word | Meaning |
|---|---|
| **Fingerprint** (*impronta*) | a jump of power seen on the main meter, with its typical duration: *+1.8 kW for 7 min* |
| **Profile** (*profilo*) | the mode an appliance runs in: *ABS print*, *Laundry Dry*, *Eco 50°* |
| **Program** (*programma*) | one kind of run of an appliance on a fixed plug, recognised by duration and kWh |
| **Base load** (*carico di base*) | what the house uses at night with everything "off": router, fridge, standby |
| **Baseline** (*bianco*) | the base load measured by hand, with everything switched off |
| **Profiler** | the roaming metering plug used to teach Wattson |
| **Fixed plug** (*presa fissa*) | a metering plug that always stays on the same appliance |
