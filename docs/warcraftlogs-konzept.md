# Ein Warcraft-Logs-Konzept fuer alle Fights

Stand **2026-09-12**, erhoben gegen `origin/main` = `582f3b6` (#166) und gegen die
committeten Dokumente unter `web/public/data/MID2/` -- `fights.json` vom Lauf am
12.09.2026 (`c8be2e2`, der erste Lauf nach der Zwillings-Reparatur #160),
`spawns.json` vom 10.09., `progress-hours.json` vom 06.09.

Jede Zahl hier stammt aus einer committeten Datei, aus einem `measurement.cost`-Block
oder aus dem Quelltext, mit Fundstelle. Wo nichts gemessen ist, steht **UNMEASURED**
und keine Zahl. Wo gerechnet wurde, steht **gerechnet**; wo geraten wurde, steht
**geschaetzt** -- und dann ist es keine Entscheidungsgrundlage.

Der Auftrag des Besitzers (12.09.2026) verlangt drei Detailstufen, einen gemeinsamen
Speicher und die Regel, dieselben Daten nie zweimal zu holen. Dieses Dokument nimmt
alle drei an und verschiebt zwei der drei Beispielzahlen, weil der Bestand sie nicht
traegt.

Die vier Fragen, die nur der Besitzer beantworten konnte, sind am selben Tag
beantwortet und stehen in Kapitel 9. Eine davon -- der Umfang von Stufe 3 -- faellt
genau auf das, was der Kohorten-Seed schon haelt, und macht damit ein zweites
privates Repo entbehrlich.

---

## 1. Was heute passiert

Elf Abrufstellen, verteilt auf fuenf Kommandos und vier Caches. Die Kostenspalte ist
gemessen, wo ein `cost`-Block existiert, und sonst UNMEASURED.

| Abrufer | Frage | Abfrage | Granularitaet | Cache | Zeitplan | gemessene Kosten |
|---|---|---|---|---|---|---|
| `fight-probe` Rankings-Anker | Welche Kills gibt es, welcher ist der frueheste? | `characterRankings` | Encounter x Difficulty x Seite, **40 Seiten** Vorgabe | `fight-probe/cache` | cron `25 * * * *` | im Lauf enthalten |
| `fight-probe` Zwilling | Ist `53421` derselbe Boss wie `3421`? | `worldData.encounter(id){name}` | Encounter-Id | dito | dito | UNMEASURED einzeln |
| `fight-probe` Reportsuche | Welche oeffentlichen Logs der Zone enthalten einen Kill? | `reports` + `ReportKills` | **1 Dokument je REPORT**, bis 500 | dito | dito | 29,1 P fuer 4 leere Encounter (Lauf 34610565576) |
| `fight-probe` Kill lesen | Zielzahl, Add-Wellen, Phasen, Auren | `FIGHT_STRUCTURE` + 4 Eventstroeme + 2 Tabellen | je Kill bis 83 Abfragen | dito | dito | **1.980,8 P / 1.190 Abfragen** (`fights.json`) |
| `spawn-probe` Kill-Auswahl | Welche Kills lese ich fuer Positionen? | `characterRankings`, **Seite 1** | Encounter x Difficulty | `spawn-probe/cache` | nur Dispatch | im Lauf enthalten |
| `spawn-probe` Zwilling | dieselbe Frage wie oben | `worldData.encounter(id){name}` | **zweimal** je Lauf | dito | nur Dispatch | UNMEASURED einzeln |
| `spawn-probe` Kill lesen | Wo erscheinen die Add-Kopien? | `FIGHT_STRUCTURE` + `EVENTS_WITH_RESOURCES` | je Kill | dito | nur Dispatch | **432,26 P / 404 Abfragen** (`spawns.json`) |
| `progress-hours` | Wie lange hat eine Gilde gebraucht? | `fightRankings(progress)` + Report-Walk | Boss x Gilde | keiner auf Platte | nur Dispatch | **694,0 P / 113 Abfragen** (`progress-hours.json`) |
| `harvest-builds` | Welche Skillung spielen echte Spieler? | Rankings + `playerDetails` + Talent-Codes | 2 Abfragen je Kill | `.work/harvest-cache` | nur Dispatch | **99,9 P / 58 Abfragen** (`harvested-builds.json`) |
| `logs-verification` | Wie steht simc zum Median echter Parses? | `characterRankings` mit Klassen/Spec-Filter | (Klasse,Spec) x Encounter = **208** | **KEINER** | cron `0 6 * * 1` | **UNMEASURED** |
| `fight-zones` | Welche Raids rankt WCL gerade? | `worldData.zones` | 1 Abfrage fuers ganze Spiel | keiner | nur Dispatch | UNMEASURED, 1-2 Abfragen |

Dazu die Budget-Klammer `rate_limit()`, absichtlich **nie** gecacht
(`warcraftlogs.py:838`, `cache=False`) -- sonst haetten beide Lesungen denselben
Schluessel und jeder Lauf meldete 0 Punkte.

### Punkte sind Felder, nicht Bytes und nicht Requests

Das ist das Kostenmodell, und es ist im Schwesterprojekt direkt gemessen
(`wtt-backend/docs/WCL-PARSES.md:45-49`): *"80 zoneRankings cost 80 x 5.0 whether they
arrive in one document or eighty"*, bei einem **Bodensatz von ~1,0 Punkt je
Dokument**, und *"batching buys round trips, not points"*. Die Gegenprobe auf der
Fight-Seite, aus den `cost`-Bloecken gerechnet:

```
fight-probe        1.980,8 P / 1.190 Abfragen = 1,66 P je Abfrage
spawn-probe          432,26 P /   404 Abfragen = 1,07
harvest-builds        99,9 P /    58 Abfragen = 1,72
progress-hours       694,0 P /   113 Abfragen = 6,14
```

Faktor 5,7 zwischen der billigsten und der teuersten Abfrageart, obwohl alle vier
GraphQL-Dokumente sind. Der Unterschied ist der Feldmix, nicht die Antwortgroesse:
`progress-hours` loest je Zeile eine Gilde auf, `spawn-probe` holt Eventseiten, die
ihren Bodensatz kosten, ob sie 80 oder 10.000 Events tragen.

**Folge fuer jedes Speicherkonzept:** ein gemeinsamer Speicher spart Punkte nur dort,
wo er das erneute Aufloesen von Feldern vermeidet -- und er erzeugt dabei ein
Byteproblem, das es vorher nicht gab. Wer "Punkte sparen" als Ziel setzt, optimiert
auf der Fight-Seite eine Groesse, die im Monat zu ueber 98 % brachliegt (siehe
Kapitel 6). Was ein Speicher wirklich kauft, ist **Planbarkeit**: eine teure Stunde
einmal zahlen statt taeglich, und eine Auswertung aendern, ohne sie neu zu bezahlen.

### Der eine Abrufer ohne jede Messung

`logs-verification` laeuft woechentlich per cron, baut seinen Client ohne `cache_dir`
(`warcraftlogs.py:1372`), bekommt im Workflow weder `--cache` noch einen
`actions/cache`-Schritt, ruft `rate_limit()` nie auf und schreibt keinen
`cost`-Block. Umfang gerechnet aus den committeten Dateien: `index.json` haelt 51
Builds mit **26 verschiedenen (Klasse, Spec)-Paaren**, `fight_profiles.json` haelt
fuer MID2 **8 Encounter** -- also **208 Ranking-Abfragen je Lauf, jede ohne
Wiederverwendung**. Punktwert UNMEASURED. Das ist der einzige punkteverbrauchende
Abrufer mit Zeitplan und ohne Messung, und die Luecke besteht unabhaengig davon, ob
dieses Konzept gebaut wird.

---

## 2. Wo heute dasselbe zweimal geholt wird

Nur belegte Faelle. Jede Zahl ist an `web/public/data/MID2/fights.json` bzw.
`spawns.json` nachgezaehlt.

### 2.1 Derselbe Raid-Abend, mehrfach geoeffnet -- 22,5 %

`FIGHT_STRUCTURE_QUERY` ist auf `(code, encounterId, difficulty)` geschluesselt,
obwohl `title`, `startTime`, `phases` und `masterData` **nur am `code` haengen**;
allein das Teilfeld `fights(encounterID:, difficulty:)` braucht den Filter.

Gezaehlt ueber alle `measurements[].reports[]` in `fights.json`:

```
151 Report-Nennungen
117 verschiedene Reportcodes
 34 Nennungen sind ein zweites oder drittes Oeffnen desselben Abends  = 22,5 %
151 verschiedene (encounter, difficulty, code)-Tripel                 -> kein Cache-Treffer
```

Wert der Ersparnis, gerechnet mit den 1,66 P je Abfrage desselben Laufs:
**56,6 Punkte von 1.980,8 = 2,9 % eines Laufs.** Das ist die ehrliche Groesse; die
22,5 % sind Dokumente, nicht Punkte, und die beiden duerfen nicht verwechselt werden.
Was mit dem Umbau passieren wuerde, steht in Kapitel 8 unter den Absagen.

### 2.2 Derselbe Kill, mehrfach hochgeladen -- 39,7 %

Warcraft Logs indexiert Uploads, nicht Raid-Abende. In `fights.json` gezaehlt:

```
151 gesampelte Zeilen  ->  91 verschiedene Kills   = 60 Doppel-Uploads (39,7 %)
```

Schaerfster Einzelfall: **Vashnik the Malignant auf Mythic, 6 Zeilen, 1 Kill.**
The Lost Explorers auf Mythic: 9 Zeilen, 3 Kills. Das ist der groesste einzelne
Doppelabruf des ganzen Subsystems -- und er wird heute **erst nach** dem
Eventabruf erkannt (`fightdataset.group_duplicate_uploads`), also nachdem er bezahlt
ist.

### 2.3 `characterRankings` aus vier Quellen, in drei Caches

`fight-probe` (`fightprobe.py:245`), `spawn-probe` (`addspawns.py:837`) und `harvest`
(`harvest.py:1566`) bauen bei Seite 1 ein **byte-identisches Dokument mit identischen
Variablen** -- also denselben Cache-Schluessel -- und legen es in drei getrennte
`actions/cache`-Eintraege (`fight-probe-<tier>-`, `spawn-probe-<encounter>-<npc>-`,
`.work/harvest-cache`). `logs-verification` stellt dieselbe Frage nach Spec zerlegt
und hat ueberhaupt keinen Platten-Cache.

### 2.4 Vierzig garantiert leere Ranking-Seiten je Pass

`fightprobe._select_kills` laeuft `for page in range(...)` ohne Abbruch bei leerer
Seite; `harvest.gather_rankings` hat genau dafuer bereits ein `break`. Der Workflow
gibt `rankings_pages: 40` vor. Vier der acht MID2-Ids sind PTR-Ids ohne einen
einzigen gerankten Parse -- das steht woertlich in `fights.json`:
*"encounter 53421 is a PTR id with no ranked parses"*. Also **4 Bosse x 40 Seiten =
160 Abfragen je Pass, wo 4 genuegen wuerden.** Nach dem ersten Mal Cache-Treffer,
solange der `actions/cache` nicht verdraengt wird -- der erste Treffer je Saison
kostet, Punktwert UNMEASURED.

### 2.5 Die Zwillingsfrage, viermal gestellt und nirgends notiert

`harvest.choose_encounter_id` wird von `fightprobe`, `addspawns`, `harvest` und dem
`progress-hours`-Pfad aufgerufen -- letzterer ueber `progresshours.ENCOUNTER_ZONE_QUERY`,
ein **zweites Dokument** mit demselben Inhalt wie `warcraftlogs.ENCOUNTER_ZONE_QUERY`.
Zwei Dokumenttexte heissen zwei Cache-Schluessel: sie koennten sich auch in einem
gemeinsamen Cache nie bedienen. `spawn-probe` zahlt dabei eine vermeidbare Abfrage
extra, weil `_kill_candidates` das Ranking-Payload samt `name` holt und wegwirft,
waehrend `fightprobe._name_of` denselben Namen gratis daraus nimmt.

Die Antwort steht heute nur in **Ausgabedateien**, die kein Abrufer als Eingabe
liest: `spawns.json` (`encounterId 3421`, `filedAs 53421`) und als Prosa-Caveat in
`fights.json`. `fight_profiles.json` hat kein Feld dafuer.

### 2.6 Die Spieler-Schadenstabelle: 151 Abfragen fuer 151 Zahlen

`fightprobe.py:662` holt `fight_table(..., view_by="Source")` je Kill. Einziger
Verbraucher ist `fightextract.active_time_fraction`
(`fightextract.py:924-942`), das den **Median** von `activeTime / duration`
zurueckgibt -- ein Skalar. Bei 151 gesampelten Zeilen sind das 151 Tabellen fuer 151
Gleitkommazahlen, und jede Tabelle traegt jeden Spielernamen des Pulls (Kapitel 4.4).

### 2.7 Was NICHT doppelt geholt wird -- gegen die Annahme geprueft

Die Vorlage *"`fight-probe` und `spawn-probe` lesen beide Events derselben Kills von
The Twin Fangs"* ist heute **falsch** und wurde nachgezaehlt: `fights.json` hat auf
Twin Fangs Mythic **1** Kill, `spawns.json` **36**, Schnittmenge **0**. Von 36
Reportcodes in `spawns.json` taucht **genau einer** in `fights.json` auf --
dort unter The Coiled Altar. Ein Raid-Abend, zwei Bosse, zwei
Caches. Die Verschwendung ist real, aber sie ist **nicht** dieselbe Eventseite
zweimal; sie ist dieselbe `FIGHT_STRUCTURE` zweimal.

---

## 3. Die drei Stufen

Die Namen sind neu, die Inhalte sind das, was die Abrufer heute schon holen. Zu jeder
Stufe steht, ob der Bestand die Beispielzahl des Besitzers traegt.

### Stufe 1 -- Rohschnitt (alles, Events inklusive)

**Was drin ist:** `FIGHT_STRUCTURE` plus alle Eventstroeme je Kill, Positionen
inklusive. **Fuer welche Frage:** jede Kurve ueber die Zeit -- Zielzahl-Band,
Add-Wellen, Aura-Fenster, Spawn-Positionen -- und die eigentliche Begruendung des
Besitzers, *"um darauf analysen zu fahren"*: Auswertungen, die noch niemand
geschrieben hat.

**Kosten, gemessen:** `spawn-probe` liest 36 Kills eines Bosses fuer **432,26 Punkte
ueber 404 Abfragen** = **12,0 Punkte je Kill**, bei einem Stream und
`--max-pages 40`, `killsTruncated 0`. Der Fight-Probe-Lauf liest 151 Zeilen mit vier
Stroemen fuer 1.980,8 Punkte bei 3.084 Cache-Treffern -- nicht direkt vergleichbar,
weil drei Viertel aus dem Cache kamen.

**Die Zahl des Besitzers verschiebt sich, und das ist ein Befund ueber den Tier, nicht
ueber die Methode.** "Die ersten 100 Progresskills je Boss auf Mythic" waeren 800
Kills. MID2 hat auf Mythic ueber alle acht Bosse zusammen **18 verschiedene Kills**
(2, 1, 2, 6, 1, 3, 0, 3), und CLAUDE.md haelt fuer Vashnik fest, dass
`searchExhausted` gesetzt ist: *"No amount of `--reports` produces a seventh Mythic
kill of a boss that has six."* Auf einer **jungen** Saison ist 100 nicht erreichbar,
weil es die Kills nicht gibt. Auf einer **abgeschlossenen** Saison ist es erreichbar
-- der Kohorten-Seed hat 30.586 Zeilen ueber fuenf Zonen geliefert (CLAUDE.md,
12.09.2026).

Also: Stufe 1 ist **"bis zu N, was immer existiert"**, und die Stichprobengroesse
gehoert in das Dokument, nicht in die Annahme. Genau das tut `distinctKills` heute
schon.

### Stufe 2 -- Gestalt (je Kill, ohne Events)

**Was drin ist:** `title`, Zeiten, `phases`, `masterData`, und je Fight `size`,
`friendlyPlayers`, `enemyNPCs`, `phaseTransitions`. Beantwortet Raidgroesse,
Kampflaenge, Phasengrenzen und die Zahl der Add-Kopien **ohne ein einziges Event**.

**Fuer welche Frage:** die Faktenpromotion lebt praktisch nur davon --
`fightLengthSeconds` und `raidSize` kommen aus diesem Block und sind auch aus einem
abgebrochenen Eventabruf promotierbar. Dazu die Deduplizierung der Doppel-Uploads.

**Die Zahl des Besitzers ist hier zufaellig exakt richtig, und zwar aus einem Grund,
den er nicht gemeint haben kann.** "Die Top 1000" ist genau die Grenze, die die API
zieht: `fightRankings` liefert 50 Zeilen je Seite und **verweigert Seite 21**
(`wtt-backend/CLAUDE.md`, #259, live gemessen). Rang 1000 ist keine Wahl, sondern
eine Wand. Wer mehr will, braucht den Realm-Slice-Weg, den #259 gebaut hat.

**Kosten:** UNMEASURED als eigene Stufe -- `FIGHT_STRUCTURE` wird heute nie ohne
nachfolgenden Eventabruf geholt. Bezugsgroesse: 1,66 P je Abfrage im
Fight-Probe-Lauf.

### Stufe 3 -- Verzeichnis (alle Kills, sehr wenige Details)

**Was drin ist:** `REPORT_KILLS_QUERY` -- je Report alle Kills mit
`encounterID, difficulty, kill, startTime, endTime` -- plus die Reportliste je Zone
und Fenster.

**Fuer welche Frage:** jeder Abrufer, der **auswaehlt** statt auswertet.

**Dies ist die TEUERSTE Stufe in Punkten, nicht die billigste, und ein frueherer
Entwurf hatte es genau anders herum.** `REPORT_KILLS_QUERY` nimmt exakt eine
Variable, `$code` (`warcraftlogs.py:141`), und `fightprobe` ruft sie **in der Schleife
ueber Reports** auf. Der Kommentar daneben sagt woertlich *"one `report_kills` per
report serves every boss"* -- je **Report**, nicht je Zone. Die Ersparnis des
fehlenden Encounter-Filters ist real und gilt **quer ueber die Bosse eines Reports**,
nicht quer ueber die Reports.

Damit skaliert Stufe 3 mit der Zahl der hochgeladenen Reports einer Saison -- und
**diese Zahl hat niemand gemessen**, weil die Suche bei
`report_pages 5 x report_limit 100 = 500 Reports` je Encounter deckelt. "Alle Kills"
im Wortsinn ist heute nicht bepreisbar. Was bepreisbar ist: 500 Reports je Encounter
sind 500 Dokumente, bei 1,66 P je Abfrage **gerechnet ~830 Punkte** -- 4,6 % einer
18.000er Stunde, 23 % einer 3.600er.

**Entscheidung:** Stufe 3 wird **je Zone einmal** aufgebaut und danach nur
fortgeschrieben, nie je Boss neu. Ein "alle Kills"-Verzeichnis ueber mehrere Saisons
braucht vorher die Messung aus Kapitel 9.

---

## 4. Der Speicher

### 4.1 Kein einzelner Speicher, aber ein einziger Schluesselraum

Der Auftrag fragt nach einem Speicher. Der Bestand traegt das nicht, und der Grund ist
messbar: die Stufen unterscheiden sich um Groessenordnungen in den Bytes und um
Faktor 5,7 in den Punkten je Abfrage. Ein Medium, das fuer Stufe 3 richtig ist
(versioniert, diffbar -- eine Zeile, die sich aendert, ist ein Befund), ist fuer
Stufe 1 ausgeschlossen.

**Was eins sein muss und heute nicht eins ist, ist der SCHLUESSELRAUM.** Das ist der
eigentliche Entwurf; die Regale sind Konsequenz.

### 4.2 Die Regale

**Regal A -- Katalog (Stufe 3 + Stufe 2, ohne Namen): privates Git-Repo.**
Das Muster steht bereits und ist erprobt: `Wild-Things-Tools/wtt-progress-data`,
fine-grained PAT, `/data/` gitignored mit sechs Zeilen Begruendung,
`permissions: contents: read` im oeffentlichen Workflow. Append-only JSONL je
(Zone, Difficulty), Manifest ohne Laufstempel, atomare Schreibvorgaenge,
`--validate` als Commit-Gate. **Kosten: 0 EUR.**

Warum privat, obwohl der Katalog keine Namen traegt: weil `masterData` sie traegt und
die Trennlinie **strukturell** sein muss, nicht prozedural (4.5).

**Regal B -- Rohschnitt (Stufe 1): bleibt der `actions/cache` plus Artefakt.**
Kein neues Medium. **Kosten: 0 EUR** -- Runner, Artefaktspeicher und Cache sind fuer
ein oeffentliches Repo unentgeltlich.

**Regal C -- der veroeffentlichte Datensatz** unter `web/public/data/`: unveraendert,
und ausdruecklich **kein** Ziel fuer Roh- oder Kataloginhalte.

### 4.3 Der Schluesselraum

Der heutige Schluessel ist `sha256(json({q: Dokumenttext, v: Variablen}))`
(`warcraftlogs.py:750-756`). Er bindet an das **Kommando**, nicht an die **Sache** --
daraus folgt jede Doppelung aus Kapitel 2.3 bis 2.5. Die Regel:

> Der Schluessel ist `(Ressourcenart, natuerlicher Schluessel, Schemaversion)`,
> nie der Abfragetext.

```
encounter/<id>/<abgefragt-am>        Name, Zone, frozen -- NICHT unveraenderlich, siehe unten
zone/<id>/w<von>-<bis>/p<n>          Reportliste eines Fensters
report/<code>/e<enc>/d<diff>         Stufe 2, MIT Filter -- siehe Kapitel 8
report/<code>/actors                 Struktur: id, type, petOwner; Namen NUR fuer type != Player
report/<code>/f<id>/<stream>[+res]   Stufe 1, Seiten zusammengefasst
report/<code>/f<id>/active-time      der Median, nicht die Tabelle
kill/<zone>/<enc>/<diff>/<startMs>-<len>   Kill-Identitaet, siehe Kapitel 5
```

**`encounter/<id>` ist NICHT unveraenderlich, und ein frueherer Entwurf hat das
behauptet.** Zwei zeitabhaengige Groessen stecken darin. `frozen` dreht, wenn die
naechste Zone oeffnet -- der Code hat dafuer bereits einen eigenen Parameter, um den
Cache zu **umgehen** (`warcraftlogs.py:1052`: *"`cache=False` for a caller asking what
the zone is NOW -- its `frozen` flag turns when the next zone opens, and a cached
answer would keep saying live"*). Und die Zwillingsantwort haengt an
`has_ranked_parses`, das sich beim Saisonstart umdreht: MID3 wird wie MID2 aus einer
PTR-Zone geseedet, die Substitution feuert auf den Live-Zwilling der **vorigen**
Saison, und ein eingefrorener Eintrag liest danach fuer immer die alte Saison. Genau
der Fehler, den dieses Projekt zweimal gemacht hat (*"The nine bosses filed under MID2
are last season's raid"*).

Deshalb traegt dieser Eintrag das Abfragedatum im Schluessel und eine
Gueltigkeitsdauer, die dem Vorbild aus `progresssweep.skip_reason` folgt: eine
**frozen** Zone 168 h, eine **live** Zone 6 h. Jeder Eintrag traegt ausserdem
`schemaVersion` und `fetchedAt`.

### 4.4 Die Grenze zwischen oeffentlich und privat

**Der Befund, der diese Grenze traegt, laeuft der Erwartung entgegen: Personenbezug
und Detailstufe verlaufen ANTIPARALLEL.** Stufe 1, die groesste, traegt
report-lokale Actor-Ids. Die Namen stecken in den **kleinen** Stufen. Wer "je
detaillierter, desto heikler" annimmt, schuetzt das Falsche.

Was veroeffentlicht ist, wurde Feld fuer Feld aufgezaehlt:

| Datei | Befund |
|---|---|
| `fights.json` | **sauber.** Jeder `actorName` ist ein NPC (Axegrinder, Blood of Ula'tek, ...). 117 Reportcodes. Kein Spieler-, Gilden- oder Realmname. |
| `spawns.json` | **sauber.** 36 Reportcodes, NPC-Namen. |
| `logs-verification.json` | **sauber.** Nur Aggregate. |
| `progress-hours.json` | **NICHT sauber -- 70 verschiedene Warcraft-Logs-Gilden-IDs**, je mit `outcome` und `reportsSeen`. Siehe unten. |
| `harvested-builds.json` | pseudonym: je Zeile `report`, `fightID`, `actorID`, `killedAt` auf die Sekunde, `itemLevel` und das **volle Kit**. |

**Der eine harte Befund, heute, ohne dass dieses Konzept gebaut wird:**
`web/public/data/MID2/progress-hours.json` liegt im **oeffentlichen** Repo und traegt
unter `bosses[].guilds[].id` **70 verschiedene Gilden-IDs** (44, 312, 830, 1031,
1546, 2947, 6738, ...). Eine Gilden-ID loest ueber die WCL-API in Name und Realm auf
-- also derselbe Quasi-Identifikator, den dieses Projekt beim Reportcode korrekt
benennt. Der Satz *"Die Progress-Seite bleibt unberuehrt"* aus einem frueheren Entwurf
ist damit widerlegt. Das gehoert repariert, unabhaengig vom Konzept (Schritt 0).

**Die zweite Stelle ist das Artefakt, und dort ist es schwerer.** `fight-probe.yml`
laedt `path: fight-probe` hoch, `spawn-probe.yml` `path: spawn-probe` -- beide **den
ganzen Ordner**, in dem per Vorgabe der rohe Response-Cache liegt. Darin:

- `FIGHT_STRUCTURE` liefert `report.title` (traegt regelmaessig den Gildennamen) und
  `masterData.actors { id gameID name ... }` **ungefiltert** (`warcraftlogs.py:327`)
  -- jeden Charakternamen des ganzen Raid-Abends;
- `fight_table(view_by="Source")` ist die Schadenstabelle der Spieler, mit Namen;
- `characterRankings`-Zeilen tragen `name`, `guild`, `server`.

14 Tage Aufbewahrung, fuenf bis sechs Laeufe am Tag, oeffentliches Repo, jeder kann
Artefakte herunterladen. **Das ist die Stelle, an der die Regel "Gildennamen, Realms,
Spielernamen duerfen nie im oeffentlichen Repo landen" heute nicht haelt.**

Dass davon nichts gelesen wird, ist gemessen und nicht angenommen:
`friendly_source_ids` liest `id`, `type`, `petOwner` (`fightextract.py:979-988`);
`active_time_fraction` liest `activeTime`; und **`report.title` hat ueber alle
Python-Dateien hinweg NULL Leser** -- ein Feld, das den Gildennamen traegt, wird
geholt, gecacht, hochgeladen und von keiner Zeile gelesen.

### 4.5 Warum die Antwort trotzdem nicht "beim Schreiben verwerfen" ist

Ein frueherer Entwurf wollte den Personenbezug beim Schreiben herausfiltern und den
ganzen Bestand danach oeffentlich ablegen. **Geprueft und verworfen**, aus drei
Gruenden, und der erste allein genuegt:

1. **Der Filter kann den Response-Cache nicht erreichen.** `WarcraftLogsClient.query`
   schreibt die vollstaendige GraphQL-Antwort in die Cache-Datei, **bevor** ein
   Extraktor sie sieht. Filtert man dort, ist es kein Response-Cache mehr, sondern ein
   Extrakt -- und ein weggefiltertes Feld ist dann von einem Feld, das die API nie
   gesendet hat, nicht mehr unterscheidbar. Das ist der Fehler, den dieses Projekt
   viermal bezahlt hat (`hostilityType`, `includeResources`, `zoneID: 0`,
   `includeCombatantInfo`), eingebaut als Dauerzustand.
2. **Ein prozeduraler Schutz ersetzt keinen strukturellen.** Ein vergessener Filter
   sieht aus wie ein wirkender Filter; ein vergessenes privates Repository gibt es
   nicht. PR #161 schuetzt strukturell, und das ist der Massstab.
3. **`gameID` ist staerker als der Reportcode.** Ein Reportcode identifiziert einen
   Raid-Abend, `gameID` identifiziert einen Charakter ueber alle Reports, Gilden und
   Saisons hinweg -- und ist trivial aufloesbar, weil derselbe `gameID` in jedem
   anderen Report neben dem Namen steht. Fuer Spieler wird er wie ein Name behandelt.

**Die Antwort ist stattdessen: den Cache aus dem hochgeladenen Ordner herausnehmen.**
Zwei Zeilen, keine Auswertung betroffen (Schritt 0).

---

## 5. Die Dedup-Regel

> Zwei Abrufe sind derselbe Abruf, wenn sie dieselbe **Sache** meinen -- nicht, wenn
> sie denselben Abfragetext tragen.

Praezise, in der Reihenfolge, in der das System fragt:

1. **Kennt der Store `(Ressourcenart, natuerlicher Schluessel, Schemaversion)`?**
   Dann liefern, ohne Abfrage. Der Schluessel enthaelt **nie** den Dokumenttext, also
   teilen sich `EVENTS_QUERY` und `EVENTS_WITH_RESOURCES_QUERY` einen Eintrag, sobald
   die Resource-Variante vorliegt -- sie ist eine echte Obermenge.
2. **Ist der Eintrag noch gueltig?** Unveraenderliches verfaellt nie; Zeitabhaengiges
   (`frozen`, die Zwillingsantwort, eine Reportliste mit wanderndem Fensterende)
   verfaellt nach der Kadenz aus 4.3. Ein Schemawechsel entwertet gezielt ueber
   `schemaVersion`, und ein Filterwechsel ueber `filterVersion` -- getrennt, weil ein
   weggefiltertes Feld wie ein nie gesendetes aussieht.
3. **Traegt der Eintrag mindestens das Budget, mit dem jetzt gefragt wird?**
   Eine Eventseite aus einem 4-Seiten-Lauf ist ein **Praefix**, das wie eine
   vollstaendige Antwort aussieht. `eventBudget` und `searchBudget` machen das heute
   schon fuer den Payload; der Store-Eintrag traegt dasselbe Feld. Unbekannt ist
   nicht null: ein Eintrag ohne Budget wird neu geholt.
4. **Ist es derselbe Kill?** `kill/<zone>/<enc>/<diff>/<startMs>-<len>` -- damit
   werden die 60 Doppel-Uploads (39,7 %) **vor** dem Eventabruf erkannt statt danach.

Punkt 4 ist bewusst der letzte und wird zuletzt gebaut: `group_duplicate_uploads` ist
**kalibriert** -- 0,5 s in einem gemessenen Loch zwischen 0,082 s und 1,074 s -- und
eine Kalibrierung wandert nicht nebenbei in einen Schluessel. Der Store macht den
Schluesselraum dafuer bereit; die Zusammenfuehrung ist ein eigenes Vorhaben.

**Was der Store NICHT tut:** er ersetzt `is_complete` nicht. Die Entscheidung, ob ein
Encounter ueberhaupt angefasst wird, faellt heute vor jeder Abfrage gegen den Payload,
und das bleibt so -- sonst erzeugt eine `schemaVersion`-Erhoehung einen Miss, den
niemand nachholt, weil `is_complete` weiter "fertig" sagt.

---

## 6. Das Budget

### Ein Ledger, und es ist eine Burst-Grenze

`PointLedger` ist bereits das eine Ledger und wird es bleiben. Die entscheidungsfaehige
Groesse ist der **Stundenanteil**, nicht der Monatsanteil: gerechnet aus den
`cost`-Bloecken,

| Lauf | Punkte | bei 18.000/h | bei **3.600/h** |
|---|---:|---:|---:|
| `fight-probe` Vollpass (12.09.) | 1.980,8 | 11,0 % | **55,0 %** |
| `fight-probe` Heroic-Vollpass (CI 32992502096) | 2.769 | 15,4 % | **76,9 %** |
| `progress-hours`, EIN Boss | 694,0 | 3,9 % | 19,3 % |
| `spawn-probe` Breitenlauf | 432,26 | 2,4 % | 12,0 % |
| `harvest` Vollpass | 99,9 | 0,6 % | 2,8 % |

**Geplant wird gegen 3.600**, weil CLAUDE.md festhaelt, dass dieses Konto dort schon
beobachtet wurde (*"observed at 3,600 as well as 18,000"*). Bei 3.600 reisst der
Heroic-Vollpass allein das 0,8er-Ceiling.

Daneben steht eine **zweite** Grenze, die kein Punktebudget ist und die
`progress-hours.json` mitprotokolliert: `x-ratelimit-limit: 800`,
`x-ratelimit-remaining: 781`. Ein Anfrage-Ceiling. Dessen **Fenster ist UNMEASURED**
-- ein Heroic-Pass hat 2.616 Abfragen gesendet, ohne 429 zu bekommen, also ist 800
keine Stundengrenze. Nicht in eine Pass-Obergrenze uebersetzen, bevor jemand das
Fenster gemessen hat.

### Vorrang, wenn die Stunde knapp wird

Die Reihenfolge folgt der Frage "was ist beim naechsten Lauf noch zu haben":

1. **Die Budget-Klammer selbst.** Nie gecacht, nie uebersprungen -- ohne sie ist jede
   Ausgabe UNMEASURED.
2. **Stufe 3 fuer die laufende Saison.** Am billigsten je Erkenntnis und die
   Voraussetzung jeder Auswahl. Ein Kill, den das Verzeichnis nicht kennt, wird von
   keiner spaeteren Stufe gefunden.
3. **Stufe 1 fuer Kills, die verschwinden koennen.** Ein Report kann privat gestellt
   oder geloescht werden; eine Ranking-Zeile nicht.
4. **Stufe 2 breit.** Verliert nichts durch Warten.
5. **Alles Wiederholbare zuletzt** -- Auffrischungen, zweite Difficulties.

Bei Erreichen des Ceilings gilt, was heute schon gilt und was #159 gerade repariert
hat: **schreiben, was bezahlt ist, und mit dem Grund enden.** Ein Encounter, den das
Ceiling vor dem ersten Fight gestoppt hat, trägt **nichts** bei -- er darf keine
`fightsSampled: 0`-Zeile schreiben, die eine aeltere Messung ersetzt.

### Was das Konzept kostet und spart, ehrlich

**Einmalig:** Schritt 3 (Schluesselwechsel) entwertet bestehende Cache-Eintraege
nicht, weil der alte Cache als Hinterlegung bestehen bleibt -- das ist der Grund, es
so zu bauen. Ein Umbau, der jeden Eintrag entwertet, kostet **gerechnet ~5.133
Punkte** (3.084 Cache-Treffer x 1,66 P), also 28,5 % einer 18.000er und **143 % einer
3.600er Stunde**. Deshalb wird er nicht gemacht.

**Laufend gespart, gemessen:** die 34 doppelten `FIGHT_STRUCTURE`-Dokumente sind
56,6 Punkte je Vollpass (2,9 %), die Zwillingsfrage 3 Dokumente, die 160 leeren
Ranking-Seiten ihren ersten Treffer je Saison. Das ist die ehrliche Groesse, und sie
ist klein. **Der Ertrag dieses Konzepts ist nicht die Punktersparnis, sondern dass
eine Auswertung geaendert werden kann, ohne die Abrufe neu zu bezahlen.**

**Ein frueher als Hauptposten gefuehrter Betrag existiert nicht mehr:** die 29,1
Punkte je Leerlauf-Lauf (~4.900/Monat) sind seit dem 11.09.2026 behoben --
`searchBudget` steht im Code (`fightprobe.py:213, 368, 947, 985-992`). Nicht mehr
zitieren.

**Geld: 0 EUR.** Beide Regale liegen auf GitHub-Infrastruktur eines oeffentlichen
Repos bzw. in einem privaten Git-Repo. Cloud Run wird nicht beruehrt. Zum Vergleich,
was vermieden bleibt: derselbe Kohorten-Walk als Cloud-Run-Job kostete gemessen
**42,79 USD/Monat**, und genau deshalb ist er nach `wow-dps-breakdown` umgezogen.

---

## 7. Der Weg dahin

Jeder Schritt ist einzeln lieferbar und einzeln nuetzlich. Kein Big Bang. Der Aufwand
ist **geschaetzt** und als solcher gekennzeichnet.

**Schritt 0 -- zwei Lecks schliessen. Setzt nichts voraus.** (geschaetzt: je ~1 h)
- (a) Die 70 Gilden-IDs in `progress-hours.json` durch einen laufstabilen, nicht
  rueckrechenbaren Pseudonym-Schluessel ersetzen -- **entschieden am 12.09.2026**,
  die beiden Anforderungen an den Schluessel stehen in Kapitel 9. Nicht entfernen:
  die Zeilen tragen die Verteilung der `outcome`-Werte.
- (b) Den Response-Cache aus dem hochgeladenen Artefaktordner herausnehmen
  (`path:` auf die Payload-Datei einschraenken oder `--cache` nach `.work/` legen),
  in `fight-probe.yml` und `spawn-probe.yml`.
- (c) `spawn-probe/` und `harvest/` in `.gitignore` -- beide sind heute **nicht**
  ignoriert, `fight-probe/` ist es. Dass nichts committet wurde, liegt allein daran,
  dass der Commit-Schritt auf `web/public/data` eingegrenzt ist; ein lokaler Lauf plus
  `git add -A` schriebe die Rohantworten eines fremden Raids in die oeffentliche
  Historie.

**Schritt 1 -- die drei billigen Doppelungen.** (geschaetzt: ~3 h)
Abbruch bei leerer Ranking-Seite in `fightprobe._select_kills`, nach dem Vorbild von
`harvest.gather_rankings` (160 Abfragen je Pass). `spawn-probe` nimmt den
Encounter-Namen aus dem Ranking-Payload, das es ohnehin holt. Die beiden
`ENCOUNTER_ZONE_QUERY`-Fassungen werden eine.

**Schritt 2 -- `wclstore`: die Schluesselfunktion, Lesen und Schreiben**, vorlaeufig
mit dem heutigen Platten-Cache als Hinterlegung. Kein neues Medium, kein neues Repo.
Alle Abrufer wechseln darauf; jeder Eintrag traegt `schemaVersion`, `filterVersion`,
`fetchedAt` und sein Budget. (geschaetzt: ~2 Tage)
Das ist der eigentliche Entwurf. Er aendert nichts Sichtbares: dieselben Laeufe,
dieselben Dokumente, ein Schluesselraum statt vier.

**Schritt 3 -- ein gemeinsamer `actions/cache`-Schluessel** fuer `fight-probe` und
`spawn-probe`, nach dem Muster, das `sims`/`gear`/`buffs` beim simc-Build bereits
benutzen. Und die Zwillingsantwort einmal unter `encounter/<id>` notieren, **mit** der
Gueltigkeitsdauer aus 4.3. (geschaetzt: ~4 h)

> **Stand 12.09.2026: die zweite Haelfte ist gebaut, die erste ist gemessen und
> abgelehnt.**
>
> Gebaut: `WarcraftLogsClient.encounter` ist der einzige Sender von
> `ENCOUNTER_ZONE_QUERY` und gibt den ganzen Block zurueck, also liegt die
> Zwillingsantwort unter `encounter/<id>` mit der `frozen`-abgeleiteten Dauer. Die
> zweite Kopie in `progresshours` ist geloescht (2.5), und
> `addspawns._kill_candidates` holt seine Ranking-Seite ueber
> `client.encounter_rankings` statt das Dokument selbst zu schicken (2.3), also ist
> sie ein Eintrag mit `fightprobe`s und `harvest`s.
>
> Abgelehnt, mit Zahl: die Ereignisabrufe der beiden Proben schicken
> **verschiedene Dokumente** (`EVENTS_WITH_RESOURCES_QUERY` gegen `EVENTS_QUERY`),
> und das ist die teure Haelfte -- am 36-Kill-Lauf 34457405665 sind 36 von 404
> Abfragen Kampfstrukturen und der Rest Ereignisseiten. Ein gemeinsamer Cache kann
> also unter 10 % der Abfragen teilen, und davon faktisch nur, was beide am selben
> Kill fragen; sie sampeln aber verschieden (`spawn-probe` nach Ranking,
> `fight-probe` mit `--order public` nach Datum). Dagegen steht, dass `fight-probe`
> stuendlich laeuft und `spawn-probe` nur auf Zuruf: ein gemeinsamer Schluessel gaebe
> dem stuendlichen Lauf einen zweiten Schreiber fuer ein paar Dispatches im Monat.

**Schritt 4 -- `logs-verification` messbar machen:** `rate_limit()`-Klammer,
`cost`-Block, `--cache`. Schliesst die letzte ungemessene wiederkehrende Ausgabe.
(geschaetzt: ~3 h)

**Schritt 5 -- Regal A, der Katalog.** Stufe 3 und Stufe 2 ohne Namen als append-only
JSONL je (Zone, Difficulty) in ein privates Repo, nach dem Vertrag, den
`docs/progress-cohort.md` bereits definiert -- Manifest ohne Laufstempel, atomare
Schreibvorgaenge je Encounter, `--validate` als Commit-Gate. Hier faengt "dieselben
Daten nie zweimal holen" an, **ueber Laeufe hinweg** zu gelten statt nur innerhalb
eines Caches. (geschaetzt: ~3 Tage)

**Schritt 6 -- die Kill-Identitaet** (`kill/...`), damit die 60 Doppel-Uploads vor dem
Eventabruf erkannt werden. Braucht Schritt 5 und die Uebernahme der Kalibrierung.
(geschaetzt: ~2 Tage)

**Schritt 7 -- Stufe 1 als eigener Bestand, ZULETZT und nur auf Anforderung.**
Die Bedingung ist keine technische: erst wenn jemand eine Auswertung hat, die den
Rohbestand braucht, **und** die Messung aus Kapitel 9 vorliegt. Bis dahin ist der
Cache der Rohbestand, und er reicht.

---

## 8. Was das Konzept nicht tut

Jede dieser Absagen ist geprueft und verworfen, damit sie niemand neu erfindet.

- **KEIN einzelner Speicher**, obwohl der Auftrag danach fragt. Begruendung in 4.1:
  Faktor 5,7 in den Punkten, Groessenordnungen in den Bytes, und ein versioniertes
  Medium fuer Stufe 3 ist fuer Stufe 1 ausgeschlossen. Der Schluesselraum ist der
  Teil, der eins sein muss, und er wird es.

- **KEIN `FIGHT_STRUCTURE` ohne Encounter- und Difficulty-Filter.** Geprueft und
  verworfen: der serverseitige Filter ist die **einzige** Difficulty-Wache zwischen
  dem Finden und dem Oeffnen eines Kills (`fightprobe.py:551-560`, woertlich: *"this
  call is the only filter between finding a kill and asking to open it"*). Ohne ihn
  wird aus einem Fehler mit Meldung eine stille falsche Zahl, und ein Dokument, das
  eine Difficulty behauptet und zwei traegt, ist keine duenne Antwort, sondern eine
  falsche. Der Ertrag waere 56,6 Punkte je Pass (2,9 %). Der billigere Teil der
  Ersparnis -- den report-weiten Block getrennt vom Fight-Block zu schluesseln --
  bleibt offen und braucht zuerst die Messung aus Kapitel 9.

- **KEIN "Events immer mit Resources".** Geprueft und verworfen: der Repo-Kommentar
  (`warcraftlogs.py:383-393`) hat die Rechnung bereits gemacht und nennt zwei Gruende
  -- eine Variable an `EVENTS_QUERY` wuerde den Schluessel **jeder je gecachten
  Eventseite** aendern (gerechnet ~5.133 Punkte Nachholkosten, 143 % einer 3.600er
  Stunde), und Resources reiten auf jedem Event mit, der Strom ist also dauerhaft
  groesser. Der Store liest die Resource-Variante als Obermenge, wenn sie da ist, und
  erzwingt sie nicht.

- **KEIN oeffentliches Release-Asset fuer den Rohbestand.** Geprueft und verworfen:
  die Harmlosigkeit der Eventfelder ist **UNMEASURED** -- bekannt sind nur die Felder,
  die der Code **liest**, nicht die, die die API **sendet**; `wclschema.py`
  introspiziert `Report`, `Encounter` und die Ranking-Enums, aber keinen Event-Typ.
  Dieses Projekt hat fuer genau diesen Schluss eine Regel und einen bezahlten
  Fehlschlag (*"An absence measured on the one entry least likely to carry the thing
  is not a measured absence"*). Ein Artefakt verfaellt nach 14 Tagen; ein Release-Asset
  bleibt und ist nicht zurueckholbar. Eine Verweigerung ist besser als ein plausibles
  falsches Dokument.

- **KEIN Verwerfen personenbezogener Felder als Ersatz fuer die private Ablage.**
  Geprueft und verworfen, drei Gruende in 4.5. Das Verwerfen von `report.title` und
  der Spieler-Namen bleibt trotzdem richtig -- aber als Sparmassnahme, nicht als
  Schutzwall.

- **KEIN Beschneiden der Ranking-Zeilen auf `report`/`fightID`/`startTime`.**
  Geprueft und verworfen: `progress-sweep` braucht `guildName`, `serverSlug` und
  `serverRegion` -- sie stehen als Pflichtfelder im Vertrag
  (`docs/progress-cohort.md:51, 63`) und sind dort das **Produkt**, nicht Beifang. Ein
  frueherer Entwurf hielt das Beschneiden fuer kostenlos, weil ein `grep` ueber die
  vier Fight-Abrufer nichts fand -- er hatte den fuenften Abrufer nicht in der
  Pruefung. Dieselbe Fehlerform, die dieses Projekt als *"a scoped run cannot judge the
  baseline"* fuehrt.

- **KEIN gemeinsamer Speicher mit der Progress-Seite.** Deren Zeilen sind Gildennamen,
  Realms und Stunden; ihr Zielrepo ist privat, ihr Workflow hat
  `permissions: contents: read`. Regal A uebernimmt ihr **Muster** und teilt nicht
  ihre Dateien -- ein Katalog, der Bossformen und Gildenstunden vermischt, kann seine
  eigene Vertraulichkeit nicht mehr angeben.

- **KEINE Aenderung an der Kill-Auswahl in Schritt 2.** Drei Regeln fuer "was ist ein
  Kill" sind ein echter Befund (60 von 151 Zeilen), aber `group_duplicate_uploads` ist
  kalibriert, und eine Kalibrierung wandert nicht nebenbei in einen Schluessel.

- **KEINE Zeitverfall-Regel fuer Unveraenderliches.** Ein Fight von vor drei Monaten
  ist heute derselbe Fight. Verfallen darf nur, was sich aendern kann (4.3) -- eine
  pauschale Lebensdauer waere die bequeme Antwort und die falsche: sie wirft frische
  Fakten weg und laesst veraltete Schemata stehen.

- **KEIN Cloud SQL und kein GCS.** Geprueft und verworfen: Cloud SQL scheitert an der
  Zugangsrichtung -- ein GitHub-Runner muesste ueber das oeffentliche Internet in die
  Produktionsdatenbank schreiben, also laegen Produktions-Zugangsdaten in einem
  oeffentlichen Repo. GCS scheitert nicht am Speicherpreis, sondern am Egress, sobald
  der Bestand regelmaessig zurueckgelesen wird; die Listenpreise dafuer stammen
  **nicht** aus dem Repo und muessten vor einer Entscheidung nachgeschlagen werden.
  Beides bleibt richtig, falls die Auswertung je dorthin zieht, wo die Daten liegen.

---

## 9. Was entschieden ist, und was ungeklaert bleibt

### Entscheidungen des Besitzers, getroffen am 12.09.2026

Alle vier offenen Fragen sind beantwortet. Sie stehen hier mit dem, was sie
festlegen, und mit dem, was sie ausdruecklich **nicht** festlegen -- eine
Entscheidung, deren Bedingung auf einer ungenommenen Messung ruht, ist eine
Entscheidung und keine Messung.

1. **Die 70 Gilden-IDs in `progress-hours.json`: pseudonymisieren.** Weder
   entfernen noch stehenlassen. Was der oeffentliche Datensatz heute traegt, ist
   nachgezaehlt: **70 verschiedene IDs in genau einem Bossblock** (The Twin Fangs),
   mit den Ausgaengen `measured` 34, `no-reports` 31, `no-fights` 4, `no-kill` 1 --
   also genau die Verteilung, die den Screens ihre Glaubwuerdigkeit gibt, und der
   Grund, warum Entfernen die schlechtere Haelfte waere. Erzeuger ist
   `progresshours.guild_row`. Zwei Eigenschaften muss der Schluessel haben, und die
   zweite ist die schwerere: **laufstabil**, sonst ist eine Zeile ueber zwei
   Dokumente nicht dieselbe Gilde und die Verteilung ist nur noch eine Summe; und
   **nicht rueckrechenbar** aus dem oeffentlichen Dokument, sonst ist es eine
   Umbenennung und keine Pseudonymisierung. Ein blosser Hash der Gilden-ID erfuellt
   das erste und nicht das zweite -- der Schluesselraum ist die Menge der
   WCL-Gilden-IDs und damit abzaehlbar.

2. **Stufe 3 reicht ab The War Within, alle Seasons.** Das ist **genau der Umfang,
   den der Kohorten-Seed schon haelt**, und das ist gemessen und kein Zufall: die
   fuenf Zonen 53 / 46 / 44 / 42 / 38 sind The Venomous Abyss, VS/DR/MQD, Manaforge
   Omega, Liberation of Undermine und Nerub-ar Palace -- Midnight S1-S2 und TWW
   S1-S3, 30.586 Zeilen. Es ist also keine Zone nachzutragen. Was die Entscheidung
   **abschneidet**, ist die Historie davor: `fetch_progress_hours --seasons all`
   erreicht 16 Tiers zurueck bis Antorus (wtt-backend/CLAUDE.md), also elf weitere,
   die nicht in den Bestand kommen. Und sie beantwortet Frage 3 mit: ein zweites
   privates Repo waere **nicht** noetig gewesen -- der Umfang passt in den
   bestehenden.

3. **Ein zweiter Ordner in `wtt-progress-data`, kein zweites Repo.** Der Preis
   dieser Entscheidung steht in Kapitel 8 und bleibt stehen: Gildenstunden und
   Bossformen haben **unterschiedliche** Vertraulichkeit, und ein gemeinsames Repo
   kann das nur noch per Ordnernamen ausdruecken statt per Zugriffsrecht. Der
   Ordner traegt deshalb seine eigene `--validate`-Regel und sein eigenes Manifest,
   und keine Datei liegt in beiden. Was die Absage aus Kapitel 8 (*"KEIN
   gemeinsamer Speicher mit der Progress-Seite"*) meinte und weiter meint, ist das
   Vermischen in **einer Datei**; ein Nachbarordner unter demselben PAT ist davon
   nicht betroffen.

4. **Stufe 1 haelt vorerst alles -- solange die Kosten nicht oder kaum steigen.**
   Die Begruendung des Besitzers ist richtig in der Richtung, auf die es ankommt:
   `fights` und die WCL-Components sind **stromweit** statt feldselektiv, also
   wuerde ein verschmaelerter Bestand keinem von beiden nuetzen; und die Components
   laufen in WCLs eigener Umgebung gegen die ambienten Event-Globals, zahlen also
   je Event nichts.

   Drei Praezisierungen, ohne die der Satz mehr verspricht, als er haelt:

   - **Punkte fallen beim Abruf an, Bytes beim Behalten.** Ein bereits bezahlter
     Payload laenger aufzuheben kostet **keinen einzigen Punkt**. Die Bedingung
     *"solange die Kosten nicht oder kaum steigen"* ist damit eine Frage nach
     Bytes -- und die Byte-Zahl ist die erste ungenommene Messung im naechsten
     Abschnitt (233 MB roh oder komprimiert, Faktor ~8).
   - **`fight-probe` liest nicht alle Events eines Kills.** `--max-pages` x
     `--events-limit` begrenzt den Abruf, und CLAUDE.md haelt fest, dass diese
     Grenze echte Pulls abschneidet. "Alles aufheben" bewahrt also, was geholt
     wurde, nicht, was existiert -- ein Stufe-1-Bestand aus Probe-Payloads erbt die
     Truncation mitsamt ihrem `truncated`-Flag.
   - **Regal B haelt heute NICHT alles, und das ist nachgemessen.** `fight-probe.yml`
     und `spawn-probe.yml` setzen beide `retention-days: 14`, das Artefakt mit dem
     Rohschnitt ist also nach vierzehn Tagen weg; der `actions/cache` ueberlebt nur,
     solange er angefasst wird. Die billigste Umsetzung dieser Entscheidung ist
     deshalb **nicht** Schritt 7, sondern die Aufbewahrungsfrist -- und was dabei zu
     pruefen ist, bevor sie hochgeht, sind GitHubs Grenzen fuer Cache und Artefakte
     eines oeffentlichen Repos, die dieses Dokument nicht gemessen hat.

   Schritt 7 bleibt damit unveraendert letzter Schritt und unveraendert an seine
   zwei Bedingungen gebunden. Die Entscheidung beantwortet *"ist der Rohschnitt
   ueberhaupt etwas wert"* mit ja und nicht *"baut ihn jetzt"*.

### Messungen, die noch niemand genommen hat

- **Ist das 233-MB-Artefakt roh oder komprimiert?** `actions/upload-artifact@v4`
  komprimiert beim Upload; CLAUDE.md:6361 sagt es nicht. Faktor ~8 auf jeder
  Byte-Zahl, an der die Dimensionierung von Stufe 1 haengt. Kostet keine einzige
  WCL-Abfrage: `du -sb fight-probe`, `du -sb fight-probe/cache`,
  `ls fight-probe/cache | wc -l` im naechsten Lauf.
- **Wieviele Reports haelt eine Zone wirklich?** Die Suche deckelt bei 500
  (`report_pages 5 x report_limit 100`). Ohne diese Zahl ist "alle Kills" nicht
  bepreisbar.
- **Kostet ein ungefiltertes `FIGHT_STRUCTURE` mehr als ein gefiltertes?** Es traegt
  alle Fights des Reports statt eines. Wenn Punkte je aufgeloestem Feld abgerechnet
  werden -- und danach sieht es aus --, koennte der Report-Schluessel **teurer** sein
  als die 2,9 %, die er spart. Messung: denselben Report einmal so und einmal so,
  Punktdelta lesen. Zwei Abfragen.
- **Kostet `includeResources` Punkte?** Resources sind zusaetzliche Felder, also
  vermutlich ja. Entscheidet, wie viel der Store als Obermenge wiederverwenden darf.
  Dieselbe Zwei-Abfragen-Messung.
- **Was kostet `logs-verification`?** 208 ungecachte Ranking-Abfragen jede Woche,
  UNMEASURED. Schritt 4.
- **Welches Fenster hat `x-ratelimit-limit: 800`?** Ein Heroic-Pass sendete 2.616
  Abfragen ohne 429, also ist es keine Stundengrenze. Bis das Fenster gemessen ist,
  darf 800 nicht in eine Pass-Obergrenze uebersetzt werden.
- **Ein vollstaendig ausgeschriebener Event-Payload, alle Felder aufgezaehlt, mit
  Revision und Datum.** Das ist die Pflicht, die dieses Projekt sich fuer jede
  Abwesenheit selbst auferlegt hat, und die Voraussetzung fuer jede Entscheidung
  darueber, wo Stufe 1 liegen darf.

### Ein Beifund, der kein Speicherproblem ist

Jeder Messblock in `fights.json` traegt den Caveat *"Sampled from page 1 of the
rankings"*, obwohl `measurement.order` auf `public` steht. `fightdataset._caveats` hat
einen Zweig fuer `order == 'first'` und einen fuer `rankingsPage == 1`, aber keinen
fuer `public` -- die Workflow-Vorgabe. Fuer die vier PTR-Bosse ist der Satz zufaellig
richtig, fuer die anderen vier falsch, und zwar in der Richtung, die die Stichprobe
besser aussehen laesst.
