# ─── Keyword terms for Deepgram ──────────────────────────────────────────

TERMS_LIST = [
    "Borussia Monchengladbach", "Heidenheim", "Gladbach", "BMG", "FCH",
    "Bundesliga", "Borussia-Park", "Monchengladbach", "Fohlenelf",
    "Matchday 28",
    "Franck Honorat", "Wael Mohya", "Jens Castrop", "Shuto Machino",
    "Nico Elvedi", "Moritz Nicolas", "Kevin Diks", "Philipp Sander",
    "Yannick Engelhardt", "Rocco Reitz", "Joe Scally", "Kevin Stoger",
    "Florian Neuhaus", "Haris Tabakovic", "Hugo Bolin", "Gio Reyna",
    "Tim Kleindienst",
    "Budu Zivzivadze", "Marnon Busch", "Patrick Mainka", "Niklas Dorsch",
    "Eren Dinkci", "Jonas Fohrenbach", "Julian Niehues", "Marvin Pieringer",
    "Diant Ramaj", "Mathias Honsak", "Hennes Behrens", "Leonidas Stergiou",
    "Arijon Ibrahimovic", "Mikkel Kaufmann", "Benedikt Gimber",
    "Frank Schmidt", "Oigan Polanski", "Bastian Dankert",
    "Nordkurve", "Ruven Schroder",
    # Surnames (single tokens)
    "Honorat", "Mohya", "Castrop", "Machino", "Elvedi", "Nicolas",
    "Diks", "Sander", "Engelhardt", "Reitz", "Scally", "Stoger",
    "Neuhaus", "Tabakovic", "Bolin", "Reyna",
    "Zivzivadze", "Busch", "Mainka", "Dorsch", "Dinkci", "Fohrenbach",
    "Niehues", "Pieringer", "Ramaj", "Honsak", "Behrens", "Stergiou",
    "Ibrahimovic", "Kaufmann", "Gimber", "Dankert",
    # Extra terms Deepgram mangles
    "St. Pauli", "Sankt Pauli", "Freiburg",
    "Rheinland", "Koln", "Cologne",
    "Bosnia", "Herzegovina", "Georgian",
    "relegation", "last-gasp", "matchdays",
]

# ─── Deterministic corrections ───────────────────────────────────────────

GLOBAL_FOOTBALL_CORRECTIONS = [
    # League / competition
    ("Gundesliga", "Bundesliga"),

    # Refereeing / set-pieces. Keep these context-anchored; bare "freak"
    # can be valid football English ("freak goal", "freak deflection").
    ("Falled by", "Fouled by"),
    ("Freak it has been", "Free kick has been"),
    ("Freak has been given", "Free kick has been given"),
    (", Freak.", ", free kick."),
    (". Freak.", ". Free kick."),
    ("therefore, Freak", "therefore, free kick"),

    # Score / standings phrasing
    ("relegated battle", "relegation battle"),
    ("Rock Blossom", "Rock Bottom"),
    ("Not one a game", "Not won a game"),
    ("three hole draw", "three-all draw"),
    ("three o draw", "three-all draw"),
    ("last guest winner", "last-gasp winner"),
    ("laxed gasp winner", "last-gasp winner"),
    ("at laxed gasp", "a last-gasp"),
    ("in the lead.", "in the league."),
    ("in the lead,", "in the league,"),

    # General commentary phrasing
    ("He's on a Way through", "He's on his way through"),
    ("Fanged way back", "Banged away back"),
]

CORRECTIONS = [
    # ─── Team name misrecognitions ───
    ("Honsakovic in the blue", "Heidenheim in the blue"),
    ("Honsenheim in the blue", "Heidenheim in the blue"),
    ("Zivadze in the blue", "Heidenheim in the blue"),
    ("Flag back all in white", "Gladbach all in white"),
    ("Fanback all in white", "Gladbach all in white"),
    ("Flagback all in white", "Gladbach all in white"),
    ("Flankert all in white", "Gladbach all in white"),
    ("Flag back", "Gladbach"),
    ("Fanback", "Gladbach"),
    ("Flagback", "Gladbach"),
    ("Flankert", "Gladbach"),
    ("At Back of", "Gladbach have"),
    ("Tabakov picked up", "Gladbach have picked up"),
    ("Saks Paoli", "St. Pauli"),
    ("Saks Pauly", "St. Pauli"),
    ("Fallen Elf", "Fohlenelf"),
    # ─── Bundesliga / league terms ───
    ("Gundesliga", "Bundesliga"),
    ("Rock Blossom", "Rock Bottom"),
    ("relegated battle", "relegation battle"),
    ("in the lead.", "in the league."),
    ("in the lead,", "in the league,"),
    ("in that side,", "in that time,"),
    # ─── Score / match references ───
    ("last guest winner", "last-gasp winner"),
    ("laxed gasp winner", "last-gasp winner"),
    ("at laxed gasp", "a last-gasp"),
    ("Not one a game", "Not won a game"),
    ("three hole draw", "three-all draw"),
    ("three o draw", "three-all draw"),
    ("four seed Bundesliga", "fourteen Bundesliga"),
    ("15.27 games", "15 points from 27 games"),
    ("beat 5.21", "beat Freiburg 2-1"),
    # ─── Rival / location names ───
    ("Brightman rivals, Curl", "Rheinland Rivals, Koln"),
    ("Brightland rivals, Curl", "Rheinland Rivals, Koln"),
    ("Brightman rivals, Koln", "Rheinland Rivals, Koln"),
    ("Brightland rivals, Koln", "Rheinland Rivals, Koln"),
    ("Brightman rivals", "Rheinland Rivals"),
    ("Brightland rivals", "Rheinland Rivals"),
    ("at Brightman.", "at Rheinland Rivals, Koln."),
    ("at Brightman ", "at Rheinland Rivals, Koln "),
    # ─── Bosnia / Herzegovina ───
    ("Bolznier Herzegovina", "Bosnia-Herzegovina"),
    ("Bolznik, Honsakovic", "Bosnia-Herzegovina"),
    ("Bolznik Honsakovic", "Bosnia-Herzegovina"),
    ("heroic self pulse. Near Herzegovina", "heroics helping Bosnia-Herzegovina"),
    ("heroic self in Bosnia", "heroics helping Bosnia"),
    # ─── Player / person names ───
    ("Ubijzivzivadze", "Budu Zivzivadze"),
    ("Budu, Zivzivadze", "Budu Zivzivadze"),
    ("Mubu Zivzivadze", "Budu Zivzivadze"),
    ("Mubi Zivzivadze", "Budu Zivzivadze"),
    ("Chortion appendage", "Georgian appendage"),
    ("Georgia appendage", "Georgian appendage"),
    ("Bolt Bastian national GT in South Korea", "Bolin has been on international duty with South Korea"),
    ("Korea is fit for this one", "Bolin is fit for this one"),
    # ─── Commentary phrasing fixes ───
    ("big six in for", "this season for"),
    ("Big six in for", "This season for"),
    ("the by Engelhardt", "the captain. Forward by Engelhardt"),
    ("Falled by", "Fouled by"),
    ("Fanged way back", "Banged away back"),
    ("Bright Shuto", "Shuto"),
    ("in a run.", "in a row."),
    ("He's on a Way through", "He's on his way through"),
]


def apply_corrections(text, corrections=None):
    for wrong, right in (corrections if corrections is not None else CORRECTIONS):
        text = text.replace(wrong, right)
    return text


_SHORT_ACK_NORMALIZATIONS = {
    # In football co-commentary this is commonly agreement/confirmation, not
    # "the match is over". Keep the normalized source short so translations
    # remain a brief interjection.
    "that's it": ("That's right.", "ambiguous_acknowledgement"),
    "that is it": ("That's right.", "ambiguous_acknowledgement"),
}


def _canonical_short_text(text):
    return " ".join(
        text.strip()
        .strip("\"'“”‘’")
        .rstrip(".!?")
        .lower()
        .split()
    )


def apply_short_utterance_policy(text, audio_start=None, audio_end=None,
                                 near_final_whistle=False):
    """Normalize only high-confidence ambiguous short commentary turns.

    The policy deliberately avoids suppressing bare names and short football
    callouts. It returns (new_text, metadata) so logs can show exactly what
    happened.
    """
    original = (text or "").strip()
    meta = {
        "short_policy": "none",
        "short_policy_reason": "",
        "normalized_source": None,
    }
    if not original:
        return text, meta

    words = [w for w in original.replace("—", " ").split() if w]
    duration_s = None
    if isinstance(audio_start, (int, float)) and isinstance(audio_end, (int, float)):
        duration_s = audio_end - audio_start
    is_short = len(words) <= 3 or (duration_s is not None and duration_s <= 1.0)
    if not is_short:
        return text, meta

    canonical = _canonical_short_text(original)
    normalized = _SHORT_ACK_NORMALIZATIONS.get(canonical)
    if normalized and not near_final_whistle:
        new_text, reason = normalized
        meta.update({
            "short_policy": "normalized",
            "short_policy_reason": reason,
            "normalized_source": new_text,
        })
        return new_text, meta

    meta.update({
        "short_policy": "kept",
        "short_policy_reason": "short_callout_or_backchannel",
    })
    return text, meta
