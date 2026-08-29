# Gesamtkonzept: DPS Breakdown je Boss, je Metrik, je Skillung

Stand 2026-08-29, erhoben gegen den Code auf `main` (Daten: MID2-Datensatz vom
2026-08-28, simc 78fc816), die sieben offenen Issues und die nextpull-Seite,
anschließend adversarial gegengeprüft (vier unabhängige Prüfläufe über Fakten,
Kosten, Vollständigkeit, Kohärenz; die Korrekturen sind eingearbeitet). Jede
Zahl ist entweder eine Messung mit Quelle oder als EXTRAPOLATION markiert —
die Konvention dieses Repos gilt auch für sein Konzept.

## 1. Auftrag

Aus deiner Nachricht, in Arbeitsfragen übersetzt:

1. **Overview je Boss**: statt (bzw. neben) 1T-10T soll man einzelne Bosse
   wählen können und sehen, welche Specs dort gut sind.
2. **Boss-DMG und Overall-DMG getrennt**: im Balken beide sichtbar, plus ein
   Sortier-Toggle zwischen den beiden Metriken.
3. **Mehrere Skillungen je Spec**: heute läuft (fast) überall der eine
   simc-Hash je (Spec, Hero-Tree); andere Skillungen — insbesondere AoE- und
   per-Boss-Skillungen, auch aus Warcraft Logs (Top Boss-DPS und Top
   Overall-DPS je Boss) — sollen langfristig unterkommen und **automatisch
   erkannt oder errechnet** werden.
4. **Loot raus aus DPS Breakdown**: eigener Menüpunkt „Loot breakdown" direkt
   darunter. Dazu die Drift beheben (warum nicht alle Specs?) — **die Basis
   muss überall gleich sein**, und wenn computed besser ist als das
   Standard-Profil, soll computed überall die Basis sein.
5. **Funnel**: interessant, aber erst mit anderen Skillungen als nur ST.
6. **Grundsteine aktuell halten**: Sims und Bossfight-Analysen brauchen eine
   Dauerpflege.

## 2. Der Kernbefund vorweg

**Das Zielbild braucht fast keinen Neubau.** Die drei Achsen, die du
beschreibst, existieren als Maschinerie bereits — was fehlt, ist auf jeder
Achse etwas anderes:

| Achse | Maschinerie | Was tatsächlich fehlt |
|---|---|---|
| **Boss** (Szenario je Encounter) | vollständig: `boss_<id>`-Szenarien aus `fight_profiles.json`, `--scenario bosses` läuft im Nightly-Default, `prioritydps` wird pro Boss-Zelle extrahiert | **Daten**: MID2s 8 Encounter tragen null Facts, also expandiert `bosses` zu nichts. 10 fertige, noch nicht angewendete Promotions liegen in `fights.json` |
| **Metrik** (Boss- vs. Overall-DMG) | `priorityDps` steht in jeder Multi-Target-Zelle der Spec-Dateien; FunnelView zeichnet den Split schon | **Publikation**: das Manifest (`index.json`) trägt kein `priorityDps` je Target-Count, und beide Overviews lesen nur das Manifest |
| **Skillung** (mehrere Builds je Spec) | `extra_builds.json` + `wowdps extra-builds` materialisiert Zusatz-Builds (origin: repaired/harvested/computed) als gewöhnliche Profile, die die Sweeps automatisch mitnehmen; der Harvest zieht validierte, deduplizierte Spieler-Builds je Boss | **Konvention + Kadenz**: kein Feld für den *Zweck* einer Variante (AoE, per-Boss), keine Scoping-Regel, wo eine Variante mitläuft, Harvest läuft nur auf Zuruf, und die Auswahl „welcher Hash ist die Basis" ist nirgends als Regel verankert |

Wichtig fürs Erwartungsmanagement: **Boss-Zellen wurden noch nie
publiziert.** Die Maschinerie ist seit 2026-08-16 im Nightly, aber die neun
Voidspire-Facts wanderten am 2026-08-17 per Re-File nach MID1, das seither
nicht re-simuliert wurde — kein publiziertes Manifest (MID1 wie MID2) hat je
ein `boss_`-Szenario getragen. Der erste Lauf nach der Promotion ist also eine
Premiere, kein Wiederholungsfall; eingeplant ist ein Schema-Check, kein
Blindflug.

## 3. Achse 1: Bosse in der Overview

### Ist-Stand

- `FightProfile.to_scenario()` baut je Boss mit Facts ein Szenario
  `boss_<encounterId>` mit genau **einem** Target-Count (der gemessenen
  Zusammensetzung), gemessener Kampflänge, Add-Waves/Amplifications als
  raid_events, `funnel_baseline="patchwerk"` (fightprofile.py:440-465).
- Der Nightly fährt `--scenario bosses` mit (sims.yml:284); für MID2
  expandiert das Token zu null, weil `fight_profiles.json` für alle 8
  Encounter leere `facts` trägt.
- Die Messungen existieren aber: `fights.json` (2026-08-26) trägt für
  53445/53455/53470/53497 Mythic-Messungen mit **10 eligible Promotions**
  (Kampflängen 396/435/286/388 s, Raid-Size 20, Targets baseline 3 auf 53445
  und 2 auf 53455; auf 53470/53497 ist die Target-Promotion wegen
  Peak-Uneinigkeit zurückgehalten), für 53420/53421 nur Heroic (17/10 Kills),
  für 53429/53492 keine Heroic-/Mythic-Kills im Suchfenster (53429 hat laut
  #48 16 Normal-Kills — Normal ist per deiner Entscheidung vom 25.08. raus).
- Die React-Overview hat den Szenario-Select über `manifest.scenarios` schon
  (OverviewView.tsx:179-187), die nextpull-Overview ebenso
  (dps-overview.ts:126) — publizierte Boss-Szenarien erscheinen dort ohne
  View-Änderung, **mit einer Ausnahme**: ein Boss mit 2 Targets (Vashnik)
  fällt ohne B2 komplett aus dem Manifest und rendert leer. B2 gehört darum
  vor bzw. zeitgleich zum ersten publizierten Boss-Lauf.

### Arbeitspaket B (Boss-Achse)

1. **B1 — Promotion (sofort, kostenlos):** `wowdps fight-promote --tier MID2
   --from-fights web/public/data/MID2/fights.json --write` schreibt die 10
   Facts. Damit sind vier Bosse „echte" Szenarien (Längen ≠ 300 s), zwei davon
   mit Multi-Target-Komposition — d. h. mit `priorityDps` je Build. Der
   nächste Nightly publiziert die Zellen von allein.
2. **B2 — Summary-Schema:** `SpecResult.summary()` (dataset.py:155-171)
   emittiert `dps` nur für die festen SUMMARY_TARGETS (1/3/5/10). Ein
   Boss-Szenario bei 2 Targets fällt damit **komplett aus dem Manifest**.
   Änderung: das Summary nimmt je Szenario dessen eigene Target-Counts auf,
   plus `priorityDps` (oder `priorityShare`) und `dpsError` je Count.
   Letzteres braucht die Tie-Regel der Metrik-Achse (Abschnitt 4) — und es
   entscheidet faktisch **#103 Form 1** mit (Präzision je (scenario, targets)
   statt eines gepoolten Medians): wer B2 freigibt, gibt beides frei. Beide
   Overviews leiten ihre verfügbaren Counts dann aus dem Summary ab statt aus
   der festen Liste (OverviewView.tsx:132-139 und dps-overview.ts:134 — der
   Fix fällt zweimal an). Für die Basis-Szenarien bleibt die 1T-10T-Auswahl
   dabei **unverändert erhalten**; nur auf einem Boss-Szenario verschwindet
   sie, weil der Boss genau eine gemessene Komposition hat (Abschnitt 13).
3. **B3 — Boss-Auswahl in der View:** `manifest.scenarios` in Basis- und
   Boss-Szenarien splitten (`id.startsWith('boss_')`), zweiter Select „Boss"
   mit Boss-Icons (`bossIconUrl` existiert), beide schreiben denselben
   `scenario=`-URL-Parameter — ein State, keine zweite Wahrheit. Default
   bleibt Patchwerk („nichts ist ausgewählt, bevor etwas gezeigt wird" bleibt
   gewahrt). Anfallende Stellen in **beiden** Frontends: OverviewView.tsx und
   dps-overview.ts/.html.
4. **B4 — Difficulty-Lücke (Rest von #48, braucht deine Entscheidung):**
   53420/53421 haben nur Heroic-Kills; Promotions rechnen heute nur aus dem
   härtesten (Mythic-)Block. Deine Anzeige-Entscheidung vom 25.08. steht
   („Normal raus, Heroic links / Mythic rechts"); offen sind laut
   #48-Schlusskommentar (a) welche der drei dort formulierten
   Darstellungsformen die Site bekommt und (b) ob **Heroic-Promotions** für
   Bosse ohne Mythic-Kills zulässig sind — dann mit `difficulty` im Fakt und
   in der View benannt. 53429/53492 bleiben leer, bis Heroic-/Mythic-Kills
   existieren oder das Suchfenster geweitet wird (`--lookback-days` /
   `--report-pages` sind die in #48 benannten Hebel); die Overview zeigt
   diese Bosse schlicht nicht an, und das ist die ehrliche Anzeige.

**Kosten** (Modell in Abschnitt 10): eine Boss-Zelle im Nightly ist ein
gewöhnlicher Einzel-Sim (nur Basis-Actor, ~22 CPU-s bei 3000 Iterationen).
Die vier real promotebaren Bosse sind aber genau die überdurchschnittlichen
(drei länger als 300 s, die beiden einzigen Multi-Target-Bosse): ihre
k-Faktoren (Länge × Targets) messen sich zu 0,95-2,64, Mittel 1,77. Macht für
4 Bosse × 52 Builds ≈ **135-200 CPU-min**, mit Pet-Anteil Richtung 270
(EXTRAPOLATION aus den gemessenen Basiswerten; die idealisierte
8-Bosse-Rechnung bei k=1 läge bei ~150). Der Nightly trägt das auf 12 Shards
ohne Umbau — nur die Shard-Balance-Streuung wächst.

## 4. Achse 2: Boss-DMG vs. Overall-DMG, mit Sortier-Toggle

### Was die Daten hergeben

`prioritydps` ist genau die Zahl „Schaden auf das Haupt-Target": simc emittiert
sie, sobald mehr als ein Enemy existiert (Raid-Event-Adds zählen), und
`parse_cell` nimmt sie mit. Zwei Grenzen, die die Anzeige benennen muss:

- **Bei 1 Target ist `prioritydps == dps`** — der Split ist dort leer, Toggle
  und Zweifarbigkeit deaktivieren sich.
- **Ein Single-Enemy-Boss ohne Adds hat legitim kein `priorityDps`** (simc
  emittiert das Feld nicht). Solche Bosse zeigen nur Overall.

### Arbeitspaket M (Metrik-Achse) — beide Frontends

1. **M1 — Balkendesign:** Der Balken ist heute schon gestapelt (solide
   Klassenfarbe = simc-Messung, Schraffur = computed-Projektion), und Opacity
   trägt die Vergleichbarkeits-Flags. Beide Kanäle sind belegt; der
   Boss/Overall-Split braucht einen dritten. Zwei regelkonforme Optionen:
   - Boss-Anteil = solide Klassenfarbe, Rest = `classWash` (hellere Stufe
     derselben Hue) — Identität bleibt Klassenfarbe;
   - dem Präzedenzfall `FunnelView.SplitChart` folgen (zwei feste
     Serien-Slots, Farbe kodiert Teil-vom-Ganzen, Identität trägt die Achse).

   Vorsicht bei der ersten Option: die Projektions-Schraffur wirkt effektiv
   ebenfalls heller (die Chart-Caption nennt sie selbst „paler segment"), ein
   classWash-Segment teilt sich also perzeptuell den Helligkeitskanal mit ihr
   — das muss messbar abgesetzt oder zugunsten der SplitChart-Präzedenz
   entschieden werden; der Palette-Validator prüft Serienfarben, nicht diese
   Kollision innerhalb eines Balkens. Tabellen-Zwilling bekommt zwingend
   beide Spalten (Boss-DPS, Overall-DPS).
2. **M2 — Sortier-Toggle:** `sortBy: 'total' | 'priority'` als
   SegmentedControl; die Sortierstelle ist je Frontend eine Zeile
   (OverviewView.tsx:351 bzw. das Pendant in dps-overview.ts). Panel-Titel,
   Tooltip und Note benennen die aktive Metrik.
3. **M3 — Tie-Regel je Metrik:** „X macht am meisten auf den Boss" braucht
   `hypot(errA, errB)` über die Zell-Fehler — deshalb gehört `dpsError` je
   Count ins Summary (B2, zusammen mit #103). Kein fester Prozentsatz.
4. **M4 — Projektion nur auf der gemessenen Achse:** Die computed-Marke
   projiziert eine **Total**-Marge. Unter Priority-Sortierung werden markierte
   Zeilen nach simcs *gemessenem* `priorityDps` einsortiert; die Total-Marge
   wird **nie** auf die Priority-Achse angewendet (eine Projektion auf einer
   Achse, die niemand gemessen hat). `best.priorityDps` steht bereits in jeder
   computed-Zeile und wird heute von keinem Frontend gelesen — der
   #99-Sofortvorschlag (Boss-Verlust neben der Marke anzeigen) ist ohne neuen
   Lauf umsetzbar.

Das ist zugleich die Antwort auf **#99** in Produktform: statt einer
verdeckten Achsen-Entscheidung bekommt der Leser beide Metriken und den
Toggle; die Marke sagt dazu, was sie auf der jeweils anderen Achse kostet.
Die Frage, welche Achse **Default** ist (global und je Boss-Szenario), bleibt
deine Entscheidung — Abschnitt 11; P0 kann mit der Empfehlung shippen, der
Default ist danach eine Konstante.

## 5. Achse 3: Mehrere Skillungen je Spec

### Die richtige Struktur existiert schon

`extra_builds.json` ist genau das Register, das du suchst: eine Zelle je
zusätzlichem Build mit `profile`-Name, Basis-Profil, Hero-Tree, `origin`
(repaired/harvested/computed), Talent-Hash und Evidenz. `wowdps extra-builds`
materialisiert daraus gewöhnliche Profildateien mit Marker-Headern — und weil
sie gewöhnliche Profile sind, nehmen die Sweeps sie automatisch mit: sims,
gear, buffs, build-search und projection-check rufen die Materialisierung vor
dem Sweep auf. **Ausnahme: talents.yml tut es nicht** — ausgerechnet der
Talent-Sweep, der von mehr Builds je Spec am meisten hätte; das gehört zum
Basis-Angleich in P1. Heute: 16 MID2-Zellen, davon 9 aus dem Harvest.

Eine **zweite Skillung auf demselben Hero-Tree** (die AoE-Frage) ist
strukturell schon möglich — ein Profilname wie `MID2_Mage_Fire_Frostfire_AoE`
ergibt eine eigene Build-Id, eigene Zeile, kein Konflikt. Was fehlt, ist
dreierlei:

1. **Zweck-Konvention:** `origin` sagt nur die Herkunft, nicht die Rolle.
   Vorschlag: ein optionales Feld `variant` in der Zelle (z. B. `"aoe"`,
   `"boss:53445"`), das in den Profil-Marker und als Feld in die publizierte
   Zeile wandert, damit die Views zwei Zeilen mit demselben Hero-Tree-Label
   unterscheidbar beschriften können.
2. **Scoping-Regel — sonst explodiert der Nightly:** eine Variante als
   gewöhnliches Profil läuft heute auf *allen* Szenarien und Target-Counts
   mit. Schon eine Boss-Variante je Spec je Boss wären bis zu ~200 neue
   Zeilen × ~13+ Zellen — das Kostenmodell in Abschnitt 10 rechnet mit fixen
   52 Builds und wäre Makulatur. Die `variant`-Konvention muss darum zugleich
   festlegen, **wo** eine Variante simuliert wird: Vorschlag `boss:<id>` läuft
   nur auf `boss_<id>` plus Patchwerk-1T als Vergleichsanker, `aoe` auf
   Patchwerk 5T/10T plus 1T-Anker, ohne `variant` überall. Das bindet Anzeige
   (Entscheidung 6) und Budget zugleich — Entscheidung in Abschnitt 11.
3. **Automatische Erkennung** — der Harvest liefert sie (nächster Punkt).

### Automatik: Harvest je Boss, doppelt gerankt

Der Harvest kann heute schon je Encounter die Top-Parses ziehen, validiert
vierfach offline (kein Code zurückgegeben / dekodiert nicht /
Spec-Header-Mismatch / simcs eigene Spec-Regel), dedupliziert auf dem
**dekodierten Loadout** (nicht dem String) und publiziert `distinctBuilds` je
Spec — „ein Build" heißt also wirklich eine Skillung, egal wie viele Spieler
sie exportiert haben. Kosten gemessen: ~5 Punkte je Kill; ein Doppel-Pass
über 8 Bosse × 2 Metriken × 15 Kills ≈ 1.200 Punkte ≈ 7 % des
18.000er-Stundenbudgets — billig. (Vorbehalt: das Konto wurde auch schon mit
`limitPerHour` 3.600 beobachtet; dann sind es 33 %. Vor jedem Lauf die erste
Log-Zeile lesen.)

Für „Top Boss-DPS **und** Top Overall-DPS je Boss" fehlt nur die Metrik:
`--metric` wird als String bis in die WCL-Query durchgereicht (Default
`dps`); ein Wert wie `bossdps` wäre ohne Code-Änderung ansteuerbar, ist aber
**gegen die live API unverifiziert**, und der Workflow exponiert den
Parameter noch nicht. Erster Schritt ist darum ein Schema-Check
(`wowdps wcl-schema --type CharacterRankingMetricType` beantwortet die
Enum-Frage direkt), dann ein `metric`-Input in harvest-builds.yml. **Plan B**,
falls die Enum keine Boss-Metrik trägt: dann entfällt nur der zweite
Harvest-Pass — die Boss-Bewertung kommt vollständig aus dem eigenen
Talent-Sweep nach `prioritydps` über die per Overall-Harvest gewonnenen
Kandidaten; das ist ohnehin die eigene Messung statt WCLs Ranking.

**Der Fluss, den ich vorschlage** (je Boss, je Season wiederholbar):

```
harvest (metric=dps)      ─┐
harvest (metric=bossdps)  ─┼→ harvested-builds.json (dedupe, distinctBuilds)
                           │
                           ├→ Sichtung: neue Loadout-Keys, die kein
                           │  bestehender Build ist → Zelle in extra_builds.json
                           │  (origin: harvested, variant: "boss:<id>")
                           │
                           └→ Seeds für build-search (--harvest, verdrahtet,
                              aber noch nie im CI benutzt)
```

Damit werden neue Skillungen **erkannt** (Harvest + Dedupe: ein neuer
distinct Build, den kein Register kennt, ist die Definition von „neue
Skillung ist aufgetaucht") und **errechnet** (build-search verfeinert sie
node-set-invariant). Zwei Dinge daran sind bewusst gewählt und stehen als
Entscheidung in Abschnitt 11:

- **Die Sichtung bleibt ein manueller PR-Schritt.** Ein Register, das sich
  selbst befüllt, würde den wertvollsten Output (die Diskrepanz zwischen dem,
  was Spieler spielen, und dem, was simc shippt) stillschweigend konsumieren.
  Deine Formulierung „automatisch erkannt" ist damit erfüllt, „automatisch
  aufgenommen" bewusst nicht.
- **Die Sichtung braucht ein Kriterium**, sonst erstickt sie: der eine
  gemessene Pass lieferte bereits 168 distinct Builds über 25 Specs.
  Vorschlag: vorgeschlagen wird ein Loadout nur, wenn es (a) von ≥N der
  gesampelten Kills der Spec getragen wird (Konsens statt Exot) oder (b) im
  bossdps-Ranking vor simcs eigenem Build liegt; der PR bleibt der Gate.

**Gear-Frage dabei:** geharvestete Zellen laufen mit den Talenten des
Spielers auf dem Gear des shipped Geschwister-Profils, **wo eines existiert**
— dann vergleichbar wie simcs eigene Zwei-Build-Specs. Auf Specs ohne
shipped Profil (heute 3 der 9 harvested-Zellen: Balance Keeper, beide
Retributions) läuft die Zelle auf dem Disabled-Generator-Charakter und trägt
die Vergleichbarkeits-Flags — der Fall „kein brauchbares Geschwister-Profil"
ist also nicht hypothetisch, sondern eingetreten; für genau ihn existiert der
Gear-Anchor als Baustein (Harvest-Gear + Anchor), mit dem bekannten Preis,
dass geankerte Zahlen nie neben publizierten stehen dürfen.

### Per-Boss-Bewertung der Varianten: erst Sweep, dann Suche

- **Billig und zuerst:** `talentsweep` beantwortet genau die Frage „welcher
  der vorhandenen Builds einer Spec ist auf diesem Szenario der beste, nach
  dps UND nach prioritydps" — aus einem Lauf, bei fixem Gear. Er ist heute
  hart auf Patchwerk verdrahtet; die Parametrisierung auf ein
  `boss_<id>`-Szenario ist ein kleiner Eingriff. Kosten: der Sweep läuft je
  **Spec** (eine Invocation = Basis-Actor + die 2-3 Builds der Spec als
  Varianten), also ~26 Spec-Invocations × 8 Bosse × (22 + V·11) s × k ≈
  **4-6 CPU-h** (EXTRAPOLATION; k gemessen 0,95-2,64 auf den vier realen
  Bossen) — als Dispatch-Workflow gut tragbar. Nebenbefund: das committete
  `talents.json` ist vom 2026-08-14 (11 Specs, Targets 1/5) und damit selbst
  ein Basis-Drift-Fall — nach der Vereinheitlichung (Abschnitt 6) neu fahren.
- **Teuer und gezielt:** ein voller `build-search` je Boss wäre ≈ 8 × 52 ×
  3,9 CPU-min ≈ **27 CPU-h** (EXTRAPOLATION; die 3,9 sind Patchwerk-1T
  gemessen, der Boss-k-Faktor käme noch obendrauf — Richtung 48 CPU-h) und
  ist als Ein-Job-Lauf nicht fahrbar. Vorschlag: per-Boss-Suche nur für
  ausgewählte Zellen — z. B. die Specs, deren Patchwerk-Suche einen Sieger
  fand, oder je Boss die Top-10 — als Dispatch-Input (`--build`-Filter
  existiert).

## 6. Eine Basis überall — und computed als Basis, wenn besser

### Warum Loot heute driftet

Die Ursache ist gemessen und schon halb behoben: `gear.yml` hat die
Materialisierungs-Schritte (`unvalidated` + `extra-builds`) inzwischen — aber
**seit dem Fix lief kein Gear-Sweep**. Die committete `gear.json` ist vom
2026-08-21 und kennt 28 Builds, während die Tier-Basis 52 umfasst (das
Ranking zeigt 51 Zeilen — das refused Retribution-Default-Profil produziert
keine). Der Re-Run ist bewusst durch **#95** blockiert: `merge_gear_shards`
publiziert heute eine Dokument-Coverage/-Provenienz über drei Slots, die aus
verschiedenen Läufen stammen dürfen — nach einem Einzel-Slot-Lauf über 52
Builds stünde „Covers all 52 builds" über einer Finger-Tabelle mit 28.
Reihenfolge also zwingend:

1. **#95 umsetzen** (Coverage + Provenienz je Slot; Producer → Merge →
   Reader). Deine Entscheidung dort ist nur die Form des dokumentweiten
   Feldes.
2. **Drei Slot-Dispatches** über die volle Basis (~1.140 CPU-min gesamt —
   EXTRAPOLATION aus gemessenen Per-Spec-Kosten, der volle 52-Build-Pass ist
   nie gelaufen; Repo ist public, Actions frei, bindend ist nur Wall-Clock).
   Danach Deploy von Hand dispatchen (Workflow-Commits triggern keinen
   Deploy).

Dasselbe Muster einmal generalisiert als **Regel**: *jeder Sweep
materialisiert vor dem Lauf dieselbe Build-Liste (shipped + unvalidated +
extra_builds), und jedes publizierte Dokument trägt Coverage gegen die
Tier-Basis plus eigene Provenienz je Messeinheit.* Damit ist die Drift-Klasse
geschlossen, nicht der Einzelfall. #100 (Provenienz je (scenario, targets) in
computed-builds.json) ist laut Issue-Text „dieselbe Entscheidung" — wer #95
Punkt 1 entscheidet, entscheidet beide.

Damit die Klasse wirklich zu ist, hier die **Inventur aller publizierten
Dokumente gegen die 52er-Basis** (Stand 2026-08-28):

| Dokument | Basis heute | Angleich |
|---|---|---|
| index.json | 51 von 52 (refused Ret-Default produziert keine Zeile) | Zählbasis ausweisen; Rest ok |
| buffs.json | **52 von 52** | fertig — das Vorbild |
| computed-builds.json | 52 (Annihilator-Leiche seit 095cc58 raus) | Provenienz je Paar = #100 |
| gear.json | **28 von 52** | P1: #95 + drei Slot-Läufe |
| talents.json | 11 Specs, Stand 2026-08-14; talents.yml materialisiert nicht | P1: Workflow-Fix + Neu-Lauf |
| logs-verification.json | 192 Vergleiche über **26** Builds | P2: Neu-Lauf (wöchentlicher Cron zieht nach) |
| fights.json / harvested-builds.json | eigene Achsen (Encounter bzw. Sample) | Coverage-Felder vorhanden |

### „Wenn computed besser ist, nimm überall computed als Basis"

Das ist als **Regel mit Beleg** formulierbar, und der Beleg existiert schon:

> Ein computed Build ersetzt den shipped Hash als Arbeits-Basis einer Spec
> genau dann, wenn seine im `shipped`-Block gemessene Marge (Head-to-Head auf
> simcs eigenem Gear, nur Talente variiert) das Tie-Band schlägt.

Wichtig: **nie** die Anchor-Marge als Kriterium — der Projection-Check hat
gemessen, dass sie auf einzelnen Builds um 2,5 Punkte danebenliegt
(Devastation Scalecommander: +2,53 % anchored, +0,02 % auf eigenem Gear).
Genau die Regel benutzt die Ranking-View heute schon (`bestBuild`); sie wird
damit von einer Anzeige-Regel zur Basis-Regel befördert.

Mechanik, zwei Stufen:

1. **Sofort (ohne Sweep-Umbau):** der Gewinner-Hash wandert als Zelle
   `origin: computed` in `extra_builds.json` (passiert für 4 Zellen heute
   schon). Dann läuft er in jedem materialisierenden Sweep als eigene Zeile
   mit — Gear, Buffs, Bosse, Funnel — und ist überall sichtbar neben dem
   shipped Build.
2. **Basis-Override (dein eigentlicher Wunsch):** Sweeps, die je Spec *eine*
   Basis brauchen (Gear-Baseline, Buffs-Referenz), bekommen einen
   `talents=`-Override aus computed-builds.json (`best.talentHash`), nach der
   Regel oben ausgewählt. Der Mechanismus existiert als Präzedenzfall in
   buildsearch (`extra_options` vor den Profileset-Zeilen); als Gratis-Effekt
   repariert er auch den Base-Actor für Specs, deren Profil-Hash simc refused
   (Exit-81-Falle). Zwingend dazu: jede Zeile publiziert
   `talentsSource: "shipped" | "computed"` + Hash — sonst entsteht derselbe
   stille Basis-Defekt eine Ebene tiefer.

Empfehlung: Stufe 1 sofort, Stufe 2 danach pro Sweep einzeln verdrahtet.
Ob eine Gear-Zeile auf computed-Talenten neben einer auf shipped-Talenten in
**einem** Ranking stehen darf oder die Basis tier-einheitlich sein muss, ist
eine Vergleichbarkeits-Entscheidung derselben Art wie #99 — Abschnitt 11.

## 7. „Loot breakdown" als eigener Menüpunkt

Zwei Oberflächen, zwei kleine Eingriffe:

- **nextpull** (das ist dein „main Menu"): neuer NavNode `loot-breakdown`
  direkt hinter dem dps-Node (`main-nav/content/content.ts:80`), Route
  `/app/loot` als dünne Shell, die die bestehende `DpsLoot`-Komponente
  wiederverwendet. Sie ist standalone und hängt an `DpsDataService` und
  `DpsViewStateService` (beide `providedIn: 'root'`; die Tier-Auflösung fällt
  auf `tiers.current` zurück, also braucht die Shell keinen eigenen State und
  keine neuen Models). Der Tab „Loot" verschwindet aus `dps.ts:tabRoutes` +
  `dps.routes.ts`; ein Redirect `/app/dps/loot → /app/loot` fängt alte Links.
- **React-Site (wt-gate)**: der Tab „Loot" wandert aus der Tab-Leiste in eine
  eigene Sektion im Header (gleiches `view=`-Muster, nur visuell abgesetzt)
  — oder bleibt vorerst, wenn dir nextpull reicht. Empfehlung: beide
  Oberflächen gleich schneiden, sonst driftet die Navigation. (Dieselbe
  Zwei-Oberflächen-Frage stellt sich für Boss-Select und Toggle — Entscheidung
  8 deckt beide.)

Der Menüpunkt lohnt sich auch inhaltlich: „Loot breakdown" ist der natürliche
Ort für alles, was aus `gear.json` + den journal-abgeleiteten Pools kommt —
per-Item-Vergleich, Baseline, Ceiling/bestSets, und später die Brücke zur
Boss-Achse („dieser Boss droppt X, das ist für diese Builds ein Upgrade" —
die Journal-Loot-Tabellen je Encounter liegen über `loot-sources` bereits
vor). Das ist Ausbaustufe, nicht Teil des ersten Schnitts.

Voraussetzung, damit der neue Menüpunkt nicht mit einer 28/52-Tabelle
eröffnet: Abschnitt 6, Schritte 1-2 zuerst.

## 8. Funnel mit Multi-Builds

Kein eigener Umbau nötig — die FunnelView liest die Spec-Dateien, und jede
Variante aus `extra_builds.json` wird dort automatisch zur eigenen Zeile mit
eigenem `funnelGain`. Der Tab wird in dem Moment interessant, in dem die
Skillungs-Achse liefert:

- AoE-/Funnel-Skillungen aus dem Harvest erscheinen als eigene Balken neben
  dem ST-Build derselben Spec — dann zeigt „Funnel gain" erstmals einen
  *Build-Unterschied* statt nur einen Spec-Unterschied.
- `talentsweep` je Boss (Abschnitt 5) liefert die Tabelle „bestByDps vs.
  bestByPriorityDps disagree" pro Boss — das ist die Funnel-Erkenntnis
  („bei vier von elf Specs ist der Meter-Build nicht der Boss-Build") als
  wiederkehrende, pro Boss beantwortete Frage.

Empfohlene Reihenfolge: Funnel-Ausbau **nach** Achse 3, vorher gibt es dort
nichts Neues zu zeigen. Eine Grenze bleibt bestehen und gehört in die
Beschriftung: simcs Standard-APLs *entscheiden* sich nicht fürs Funneln
(außer Enhancement); die Zahl ist „was Funneln unter der Standard-Rotation
kostet oder bringt", nicht die Funnel-Decke.

## 9. Grundsteine aktuell halten

Jede Zeile nennt bewusst auch den **Auslöser** — alle diese Läufe sind
dispatch-only, und ein Lauf ohne benannten Auslöser findet nicht statt (die
hero_trees.json-Lektion: „Something has to invoke it or the document never
exists").

| Grundstein | Heute | Vorschlag | Auslöser |
|---|---|---|---|
| **Sims** (index + specs) | Nightly 04:00, `bosses`-Token läuft mit | unverändert — Boss-Zellen kommen mit B1 automatisch | Cron (existiert) |
| **Bossfights** (fights.json, #49) | stündlicher Cron ist nur Resume | junge Season: alle 1-2 Wochen ein Mythic-Pass. Achtung: die vier 0-Kill-Bosse gelten per `searchExhausted` als „fertig" — ein Resume (auch mit `--encounter`) öffnet sie **nicht** wieder; nötig ist `--no-resume` (Nebenwirkung: der Payload behält nur die angeforderten Encounter) oder andere difficulty/order bzw. größeres Event-Budget. Heroic-Pässe sind günstig (2.769 Punkte gemessen) und vergrößern die **Stichprobe** der Bänder 3-5x | wiederkehrendes Issue / Routine, die eine Session weckt |
| **Promotion** (fight_profiles) | manuell, bewusst | bleibt manuell (die Diskrepanz Hand vs. Messung ist der wertvollste Output), aber als fester Schritt nach jedem Probe-Pass: `fight-promote --from-fights` als PR | Teil desselben Issue-Rituals |
| **Harvest** (harvested-builds) | dispatch-only, einmal gelaufen | je Season-Start und nach jedem größeren Tuning-Pass ein Doppel-Pass (dps + bossdps) über alle Bosse; ~1.200 Punkte | Routine nach Season-/Tuning-Ereignis; `simc-changes` als Signalgeber |
| **build-search** (computed-builds) | dispatch-only | Publish-Pass nach Tuning-Pässen; per-Boss nur gezielt | dito |
| **Gear** (gear.json) | blockiert durch #95 | nach #95: je Season einmal voll (3 Slots), danach nach Tuning-Pässen | dito |
| **logs-verification** | wöchentlicher Cron, aber 26-Build-Basis | läuft nach dem Basis-Angleich automatisch auf 52 | Cron (existiert) |
| **Deploy** | manuell nach Daten-Commits | bleibt so (Workflow-Commits triggern keinen Deploy — bekannte Falle), gehört auf die Checkliste jedes datenproduzierenden Laufs | Teil des Rituals |

Nichts davon gehört auf einen engeren Cron: deterministische Sims committen
nur, wenn sich etwas bewegt hat, und die WCL-Läufe sind Judgement-Schritte
mit Punkte-Budget.

## 10. Kostenmodell

Formel (aus den gemessenen Größen; Herleitung in gear.yml-Header und
CLAUDE.md „Cost, measured"). Eine „Zelle" ist ein Einzel-Sim (nur
Basis-Actor); Varianten kommen als Profilesets obendrauf:

```
CPU_s je Invocation = (base_I + V · var_I) · (L/300) · t(T) · p
  base_3000 = 22 s   var_3000 = 11 s      (gemessen)
  base_1000 ≈ 9 s    var_1000 = 4,5 s     (var gemessen, base EXTRAPOLATION)
  t(1)=1, t(5)=3 gemessen; t(2)≈1,5, t(3)≈2 EXTRAPOLATION
  p = 1 Caster, ≤2,5 Pet (obere Schranke); L = Kampflänge
  k := (L/300)·t(T) — auf den vier promotebaren MID2-Bossen 0,95-2,64, Mittel 1,77
```

| Lauf | Umfang | Kosten (alles EXTRAPOLATION aus den gemessenen Basiswerten, sofern nicht anders vermerkt) |
|---|---|---|
| Boss-Zellen im Nightly (B1) | 4 promotebare Bosse × 52 Builds, V=0, 3000 It. | ≈ **135-200 CPU-min**, mit Pet-Anteil Richtung 270; idealisierte 8 Bosse bei k=1 ≈ 150 |
| talentsweep je Boss | ~26 Spec-Invocations × 8 Bosse × (22+V·11) s × k | ≈ **4-6 CPU-h** |
| build-search je Boss, voll | 8 × 52 × 3,9 CPU-min (gemessen bei Patchwerk 1T), Boss-k fehlt darin | ≈ **27-48 CPU-h** — nicht fahren; gezielt auswählen |
| Gear voll (3 Slots) | 52 Builds, 1T, 1000 It. | ≈ **1.140 CPU-min** (aus gemessenen Per-Spec-Kosten: Finger 8,1 CPU-min/Spec, Trinket ~12) |
| Harvest-Doppel-Pass (8 Bosse, 2 Metriken) | ~240 Kills | ≈ **1.200 WCL-Punkte** (gemessen: 5,0/Kill) |
| Heroic-Fight-Probe | 8 Bosse, 30 Reports | **2.769 Punkte** (gemessen) |

WCL-Prozentangaben gelten gegen 18.000 Punkte/h; das Konto wurde auch schon
mit `limitPerHour` **3.600** beobachtet — dann kostet der Harvest-Doppel-Pass
33 % und die Heroic-Probe 77 % einer Stunde. Vor jedem Lauf die erste
Log-Zeile lesen.

Actions-Minuten sind frei (Repo public); bindend sind Wall-Clock je Job und
die Shard-Balance — Boss-Zellen streuen mit k·p um den Faktor ~7 (k allein
~2,8, Pet-Faktor bis 2,5); die teuerste Einzelzelle (langer 3-Target-Boss,
Pet-Spec, V=3) liegt bei ~6,5 CPU-min. Die 12 Shards tragen das. Und: das
Modell rechnet mit fixen 52 Builds — jede gesichtete Variante ohne
Scoping-Regel (Abschnitt 5) vergrößert **jeden** dieser Läufe.

## 11. Entscheidungen, die bei dir liegen

1. **Achsen-Default (#99):** Overview-Default „Overall" mit Toggle auf
   „Boss" — oder auf Boss-Szenarien default „Boss"? Meine Empfehlung:
   Patchwerk-Default Overall, Boss-Szenarien-Default Boss-DMG (dort ist der
   Boss die Frage), Toggle überall sichtbar, Marke zeigt immer beide Deltas.
   P0 shippt mit der Empfehlung; der Default ist danach eine Konstante.
2. **Heroic-Rest (#48 + B4):** „Normal raus, Heroic links / Mythic rechts"
   hast du am 25.08. entschieden; offen sind nur (a) welche der drei
   Darstellungsformen aus dem #48-Schlusskommentar, (b) Heroic-**Promotions**
   für Bosse ohne Mythic-Kills — ja/nein, mit `difficulty` am Fakt.
3. **#95 Punkt 3 + Gear-Budget:** Form des dokumentweiten Coverage-Feldes und
   Freigabe des ~1.140-CPU-min-Passes. Ohne das bleibt Loot auf 28/52.
4. **#100:** Provenienz je (scenario, targets) — laut Issue dieselbe
   Entscheidung wie #95 Punkt 1. (B2 entscheidet zugleich #103 Form 1 mit —
   Präzision je Szenario im Summary.)
5. **Computed als Basis:** Stufe 1 (eigene Zeile überall) sofort — Stufe 2
   (Basis-Override in Gear/Buffs) freigeben? Gemischte Basis mit
   `talentsSource`-Ausweis je Zeile, oder tier-einheitlich? Unterpunkt
   Harvested-Gear-Standard: Geschwister-Profil-Gear, Anchor nur wo keines
   existiert (heute: Balance Keeper, beide Retributions) — ok so?
6. **Varianten-Anzeige + Scoping:** alle Skillungs-Zeilen im Ranking oder je
   Spec die beste mit Aufklapper? Und die Scoping-Regel aus Abschnitt 5
   (boss-gebundene Variante läuft nur auf ihrem Boss + 1T-Anker) — ok so?
7. **Sichtung + Harvest-Kadenz:** Sichtung manuell per PR mit dem
   vorgeschlagenen Kriterium (Konsens ≥N Kills oder vor simcs Build im
   Ranking)? Doppel-Pass je Season/Tuning freigeben; erster Schritt ist der
   Schema-Check für `bossdps`.
8. **Oberflächen-Schnitt:** Boss-Achse, Toggle und Loot-Breakdown — nur
   nextpull, nur React-Site, oder beide? (Empfehlung: beide, sonst driftet
   die Navigation; kostet die View-Arbeit doppelt.)

## 12. Phasenplan

**P0 — Boss-Achse sichtbar machen** (kein neuer Datenpfad):
B2 Summary-Schema (zuerst oder zeitgleich — sonst rendert der 2-Target-Boss
leer) → B1 Promotion → Nightly publiziert Boss-Zellen (Premiere, als
Schema-Check behandeln) → B3 Boss-Select + M1-M4 (Split-Balken, Toggle,
Tie-Regel, #99-Delta an der Marke) in beiden Frontends → B4 nach
Entscheidung 2 (blockiert nur 53420/53421, parallel zum Rest). Nebenher zwei
Kleinigkeiten aus der Erhebung: `view=buffs` fehlt im URL-Validierungs-Array
der React-Site (Buffs-Links round-trippen nicht), und die Target-Auswahl der
Overview liegt nicht in der URL — beide Einzeiler-Klasse, beide machen
Boss-Deep-Links erst teilbar.

**P1 — Basis vereinheitlichen, Loot herauslösen:**
#95 → Gear-Sweep 3 Slots → Loot 52/52 → Menüpunkt „Loot breakdown"
(Oberflächen nach Entscheidung 8) → talents.yml materialisieren lassen +
talents.json neu fahren → 51/52-Zählbasis im Manifest ausweisen.

**P2 — Skillungs-Achse:**
bossdps-Schema-Check → Harvest-Doppel-Pass je Boss → Sichtungs-PR neuer
Loadouts in `extra_builds.json` (mit `variant`-Feld + Scoping-Regel) →
View-Arbeit: `variant`-Label + Anzeigeform aus Entscheidung 6 (beide
Oberflächen) → talentsweep je Boss → computed-als-Basis Stufe 1, dann
Stufe 2 → logs-verification läuft nach dem Angleich automatisch auf 52.

**P3 — Funnel + gezielte per-Boss-Suche:**
Funnel-Tab zeigt Build-Varianten; build-search je Boss für ausgewählte
Zellen; Loot-Breakdown-Ausbau (per-Boss-Drops).

**Laufend:** Kadenzen aus Abschnitt 9, mit benannten Auslösern.

Abhängigkeiten: P0 braucht keine Owner-Entscheidung außer B4/Entscheidung 2
für zwei der acht Bosse (und shippt mit der empfohlenen Default-Achse aus
Entscheidung 1). P1 hängt an Entscheidung 3/4 und 8. P2 an Entscheidung 5-7.
P0 und P1 sind parallel fahrbar.

## 13. Grenzen, die in jede Anzeige gehören

- **simc funnelt nicht aus Entscheidung**: die Standard-APL maximiert
  Gesamtschaden; per-Boss-`prioritydps` ist „was die Standard-Rotation auf
  den Boss legt", die Funnel-Decke bräuchte Skillungs-/APL-Varianten — genau
  die Achse 3.
- **WCL-Rankings sind ranked parses only**: privat geloggte Kills sind für
  Harvest und Fight-Probe unsichtbar, in jeder Tiefe.
- **Amplification-Magnitude ist immer Assertion** — kein API-Feld sagt, was
  eine Aura tut.
- **Boss-Szenarien haben genau einen Target-Count** (die gemessene
  Komposition) — der Targets-Select verschwindet dort zu Recht; für
  Patchwerk & Co. bleibt er unverändert.
- **Ein Boss ohne Kills auf den gemessenen Schwierigkeiten hat keine Daten**
  — 53429/53492 fehlen in der Boss-Auswahl, bis Heroic-/Mythic-Kills im
  (ggf. geweiteten) Suchfenster auftauchen, und das ist die korrekte Anzeige,
  keine Lücke.
