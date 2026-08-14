/**
 * Class, specialisation and hero-talent-tree icons.
 *
 * ---------------------------------------------------------------------------
 * Why this is a presentation mapping and not a dataset field
 * ---------------------------------------------------------------------------
 *
 * simc ships no icon data at all -- established once already for items, and it
 * is just as true for classes and specs (`engine/dbc/generated/` has no icon
 * column anywhere). The dataset's value is that it is derived from simc and
 * byte-reproducible, so an externally sourced icon name must never land in it.
 *
 * Class and spec icons are a different proposition from item icons, though: the
 * mapping is small (13 classes, 39 specs, 39 hero trees), it is static across a
 * patch, and it is *ours* -- a presentation choice about how to draw a name the
 * dataset already carries. So it lives here, in the web code, keyed on the ids
 * the pipeline emits (`SpecSummary.class`, `.specId`, `.heroTalent`). Adding an
 * icon changes nothing about what was simulated.
 *
 * ---------------------------------------------------------------------------
 * Where the images come from
 * ---------------------------------------------------------------------------
 *
 * `wow.zamimg.com`, Wowhead's CDN -- the same origin the Loot view already
 * fetches item icons from through Wowhead's tooltip script, so this adds no new
 * third party. Two shapes, both verified by request on 14 August 2026:
 *
 *   class / spec  https://wow.zamimg.com/images/wow/icons/medium/<slug>.jpg
 *   hero tree     https://wow.zamimg.com/images/wow/TextureAtlas/live/<element>.webp
 *
 * The hero-tree emblems are UI *texture atlas* elements rather than icons --
 * `TraitSubTree.UiTextureAtlasElementID` in the game data, resolved through
 * `UiTextureAtlasElement` to names of the form `talents-heroclass-<class>-<tree>`.
 * All 39 return 200 at ~15 KB, 200x200 with alpha. That is why they are `.webp`
 * on a different path and not `icons/medium/*.jpg`; do not "tidy" them into the
 * icon path, they are not there.
 *
 * Nothing depends on the CDN being reachable. Every icon renders over a
 * class-coloured tile that is already on screen, and every icon carries its
 * accessible name; a blocked or offline CDN costs the picture and nothing else.
 * `ICON_BASE` / `ATLAS_BASE` are the only two lines to change to self-host.
 */

const ICON_BASE = 'https://wow.zamimg.com/images/wow/icons'
const ATLAS_BASE = 'https://wow.zamimg.com/images/wow/TextureAtlas/live'

/** Wowhead serves four sizes; `medium` (36px) is the smallest that stays sharp at 2x. */
export type IconSize = 'tiny' | 'small' | 'medium' | 'large'

const CLASS_ICONS: Record<string, string> = {
  'Death Knight': 'classicon_deathknight',
  'Demon Hunter': 'classicon_demonhunter',
  Druid: 'classicon_druid',
  Evoker: 'classicon_evoker',
  Hunter: 'classicon_hunter',
  Mage: 'classicon_mage',
  Monk: 'classicon_monk',
  Paladin: 'classicon_paladin',
  Priest: 'classicon_priest',
  Rogue: 'classicon_rogue',
  Shaman: 'classicon_shaman',
  Warlock: 'classicon_warlock',
  Warrior: 'classicon_warrior',
}

/** Keyed by the pipeline's `specId` (`mage_arcane`, `death_knight_frost`, ...). */
const SPEC_ICONS: Record<string, string> = {
  death_knight_blood: 'spell_deathknight_bloodpresence',
  death_knight_frost: 'spell_deathknight_frostpresence',
  death_knight_unholy: 'spell_deathknight_unholypresence',
  demon_hunter_havoc: 'ability_demonhunter_specdps',
  demon_hunter_vengeance: 'ability_demonhunter_spectank',
  druid_balance: 'spell_nature_starfall',
  druid_feral: 'ability_druid_catform',
  druid_guardian: 'ability_racial_bearform',
  druid_restoration: 'spell_nature_healingtouch',
  evoker_devastation: 'classicon_evoker_devastation',
  evoker_preservation: 'classicon_evoker_preservation',
  evoker_augmentation: 'classicon_evoker_augmentation',
  hunter_beast_mastery: 'ability_hunter_bestialdiscipline',
  hunter_marksmanship: 'ability_hunter_focusedaim',
  hunter_survival: 'ability_hunter_camouflage',
  mage_arcane: 'spell_holy_magicalsentry',
  mage_fire: 'spell_fire_firebolt02',
  mage_frost: 'spell_frost_frostbolt02',
  monk_brewmaster: 'spell_monk_brewmaster_spec',
  monk_mistweaver: 'spell_monk_mistweaver_spec',
  monk_windwalker: 'spell_monk_windwalker_spec',
  paladin_holy: 'spell_holy_holybolt',
  paladin_protection: 'ability_paladin_shieldofthetemplar',
  paladin_retribution: 'spell_holy_auraoflight',
  priest_discipline: 'spell_holy_powerwordshield',
  priest_holy: 'spell_holy_guardianspirit',
  priest_shadow: 'spell_shadow_shadowwordpain',
  rogue_assassination: 'ability_rogue_deadlybrew',
  rogue_outlaw: 'ability_rogue_waylay',
  rogue_subtlety: 'ability_stealth',
  shaman_elemental: 'spell_nature_lightning',
  shaman_enhancement: 'spell_shaman_improvedstormstrike',
  shaman_restoration: 'spell_nature_magicimmunity',
  warlock_affliction: 'spell_shadow_deathcoil',
  warlock_demonology: 'spell_shadow_metamorphosis',
  warlock_destruction: 'spell_shadow_rainoffire',
  warrior_arms: 'ability_warrior_savageblow',
  warrior_fury: 'ability_warrior_innerrage',
  warrior_protection: 'ability_warrior_defensivestance',
}

/**
 * Hero-talent trees, keyed by the display name the pipeline reads out of the
 * profile. Tree names are unique across classes, so the class does not have to
 * be part of the key even though it is part of the atlas element name.
 *
 * `Default` is deliberately absent: it is simc's placeholder for a spec it ships
 * one build for, not a hero tree, and inventing an emblem for it would assert
 * something the game does not contain.
 */
const HERO_TREE_ATLAS: Record<string, string> = {
  Deathbringer: 'talents-heroclass-deathknight-deathbringer',
  "San'layn": 'talents-heroclass-deathknight-sanlayn',
  'Rider of the Apocalypse': 'talents-heroclass-deathknight-rideroftheapocalypse',
  'Aldrachi Reaver': 'talents-heroclass-demonhunter-aldrachireaver',
  'Fel-Scarred': 'talents-heroclass-demonhunter-felscarred',
  'Druid of the Claw': 'talents-heroclass-druid-druidoftheclaw',
  Wildstalker: 'talents-heroclass-druid-wildstalker',
  'Keeper of the Grove': 'talents-heroclass-druid-keeperofthegrove',
  "Elune's Chosen": 'talents-heroclass-druid-eluneschosen',
  Scalecommander: 'talents-heroclass-evoker-scalecommander',
  Flameshaper: 'talents-heroclass-evoker-flameshaper',
  Chronowarden: 'talents-heroclass-evoker-chronowarden',
  Sentinel: 'talents-heroclass-hunter-sentinel',
  'Pack Leader': 'talents-heroclass-hunter-packleader',
  'Dark Ranger': 'talents-heroclass-hunter-darkranger',
  Sunfury: 'talents-heroclass-mage-sunfury',
  Spellslinger: 'talents-heroclass-mage-spellslinger',
  Frostfire: 'talents-heroclass-mage-frostfire',
  'Conduit of the Celestials': 'talents-heroclass-monk-conduitofthecelestials',
  'Shado-Pan': 'talents-heroclass-monk-shadopan',
  'Master of Harmony': 'talents-heroclass-monk-masterofharmony',
  Templar: 'talents-heroclass-paladin-templar',
  Lightsmith: 'talents-heroclass-paladin-lightsmith',
  'Herald of the Sun': 'talents-heroclass-paladin-heraldofthesun',
  Voidweaver: 'talents-heroclass-priest-voidweaver',
  Archon: 'talents-heroclass-priest-archon',
  Oracle: 'talents-heroclass-priest-oracle',
  Trickster: 'talents-heroclass-rogue-trickster',
  Fatebound: 'talents-heroclass-rogue-fatebound',
  Deathstalker: 'talents-heroclass-rogue-deathstalker',
  Totemic: 'talents-heroclass-shaman-totemic',
  Stormbringer: 'talents-heroclass-shaman-stormbringer',
  Farseer: 'talents-heroclass-shaman-farseer',
  'Soul Harvester': 'talents-heroclass-warlock-soulharvester',
  Hellcaller: 'talents-heroclass-warlock-hellcaller',
  Diabolist: 'talents-heroclass-warlock-diabolist',
  Slayer: 'talents-heroclass-warrior-slayer',
  'Mountain Thane': 'talents-heroclass-warrior-mountainthane',
  Colossus: 'talents-heroclass-warrior-colossus',
}

/** simc's placeholder for a spec it ships exactly one build for. */
export const NO_HERO_TREE = 'Default'

export function classIconUrl(wowClass: string, size: IconSize = 'medium'): string | null {
  const slug = CLASS_ICONS[wowClass]
  return slug ? `${ICON_BASE}/${size}/${slug}.jpg` : null
}

export function specIconUrl(specId: string, size: IconSize = 'medium'): string | null {
  const slug = SPEC_ICONS[specId]
  return slug ? `${ICON_BASE}/${size}/${slug}.jpg` : null
}

export function heroTreeIconUrl(heroTalent: string): string | null {
  if (heroTalent === NO_HERO_TREE) return null
  const element = HERO_TREE_ATLAS[heroTalent]
  return element ? `${ATLAS_BASE}/${element}.webp` : null
}

/**
 * Two letters to draw in the tile behind an icon that has not loaded.
 *
 * Not an identity channel on its own -- the accessible name and, nearly
 * everywhere, the written-out name beside it are. This is so a broken image
 * leaves a mark that still looks deliberate.
 */
export function iconInitials(name: string): string {
  const words = name.split(/[\s'-]+/).filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return (words[0] ?? '').slice(0, 2).toUpperCase()
  return `${(words[0] ?? '')[0] ?? ''}${(words[1] ?? '')[0] ?? ''}`.toUpperCase()
}
