# Tennis commentator v3 plan

## Status

Implemented and published on July 26, 2026 at explicit user direction. V2 had
no submitted review items and was closed with a verified zero-item
disposition. Both v3 profiles pass all deterministic, media, worst-of-three
judge, latency, placement, and rendered-speech checks. The v2 pages remain
available for side-by-side comparison, v3 is the only writable round, and the
existing Slack channel accepted the v3 announcement.

Future release title:

`AI Tennis commentator — v3 ready for review`

## Objective

Keep v2's approximately television-realistic call frequency while making
selected lines more informative. V3 should add:

- the winner of each accepted point;
- the consequence of that point in the current game;
- score-derived pressure and stakes;
- short rolling match-state analysis;
- phase-appropriate commentary based on where the players are in the game,
  set, and match;
- literal rally evidence combined with the accepted server and score.

V3 is not a license for speculative tactical commentary. It may sound more
analytical only where the score tracker, verified match context, or literal
vision output supplies the analysis.

## Evidence from television commentary

The four-broadcast, 60-minute commentary reference averages 3.37 commentator
turns per minute. Glinka–Mayo v2 already produces 3.6 calls per minute. The
difference is turn depth: the television median is 17 words versus v2's eight.

The v3 target is therefore:

- roughly 17–19 calls in this five-minute fixture, not more chatter;
- selected lines of approximately 10–18 words;
- more point outcome, score significance, and match-state narrative;
- fewer repetitive bare score statements;
- silence when the available evidence cannot support useful detail.

The TV P90 of 53 words is not a target. Adjacent captions can merge contributions
from two commentators, and long lines would create avoidable placement pressure
in the six-second profile.

## Use tennis state as the primary clock

Raw elapsed time is not enough. Two minutes of tennis can contain one long
deuce game or several short holds. Commentary selection must be driven first by
accepted tennis state:

1. point phase;
2. position within the current game;
3. position within the set and match;
4. changeover or serve setup;
5. elapsed clip time and time since the last call.

Elapsed time controls how often background material is allowed. It must never
create a tennis claim or override accepted score state.

### Required derived state

Create a pure, deterministic state layer from the previous and current accepted
`Score` objects plus accepted transition history:

- `server` and `receiver`;
- `point_winner`;
- `game_winner` and `hold_or_break`;
- `transition_type`, distinguishing a played point from a later graphic reset;
- `completed_games`;
- `point_number_in_game`;
- `consecutive_points_by_player`;
- `game_phase`;
- `set_phase`;
- `pressure_type` and `pressure_count`;
- `next_server` after game completion;
- `background_allowed`;
- evidence fields explaining every derivation.

Match-point derivation also requires a verified static
`best_of_sets: 3` match-format field. Without verified match format, v3 may say
set point but not match point.

Proposed `game_phase` values:

- `game_start` — 0-0 before the opening point;
- `opening_points` — the first one or two accepted points;
- `developing_game` — a player reaches 30 without a pressure score;
- `game_points` — server has one or more game points;
- `break_points` — receiver has one or more break points;
- `deuce` — 40-all;
- `advantage` — either player has advantage;
- `game_complete` — accepted game-count transition.

Proposed `set_phase` values:

- `opening_set` — fewer than two completed games;
- `established_set` — neither player is yet in a score-derived closing
  situation;
- `closing_set` — accepted games show that one player can close the set by
  winning the current game;
- `set_complete`;
- `unsupported_tiebreak`.

Set point may be spoken only when the player has a current game point and
winning that point would produce a valid completed-set game score. Match point
additionally requires accepted set state plus verified match format showing
that the same set would complete the match. The later graphic transition that
resets games and increments sets is not a second played point. Missing or
ambiguous set state downgrades the wording to game point. The existing tracker
does not support numeric tiebreak points; v3 must suppress tiebreak analysis
and record `unsupported_tiebreak` rather than guessing.

## Commentary policy by match and game phase

### Time into the match or set

| State | Preferred commentary | Restricted commentary |
|---|---|---|
| Opening two games | Establish players, server, handedness, and at most two short verified background facts | Do not repeat the full introduction after the opener |
| Established set | Point outcome, service-game position, accepted point runs, holds/breaks | Generic biography should become rare |
| Closing set | Game/set implications and score pressure | No unrelated background during pressure points |
| Changeover | One concise recap or verified contextual fact | No new tactical inference |
| Set completion | Set winner, accepted set score, next server if known | No match-point claim unless accepted sets prove it |

### Time into the current game

| State | Commentary priority |
|---|---|
| Before first point | Identify the server and current game score; mention first service game when relevant |
| Opening point | Name the point winner and new score |
| Second or third point | Add a verified run such as “Mayo has taken the first three points” |
| Multiple game points | State the number of chances; one background fact may fill a long pause only after the stakes have already been stated |
| Single game point | Keep the call short and focused on the point's consequence |
| Break point | Identify receiver, server, and number of break chances |
| Deuce or advantage | Use short, urgent score-state wording; suppress biography |
| Game completion | Say hold or break, game score, and next server |
| Rally | Use literal rally evidence; add current server or pressure context only when already accepted |

This policy should make commentary evolve naturally during a game. Early points
orient the viewer, middle points build the score story, pressure points focus
on consequences, and changeovers create room for context.

## Grounded analysis allowed in v3

### Score-state analysis

Allowed:

- “Mayo takes the opening point and leads 15-love on serve.”
- “Mayo has won the first three points—three game points at 40-love.”
- “Glinka saves one game point; Mayo still has two at 40-15.”
- “Mayo completes the opening hold; Glinka serves next, trailing 0-1.”
- “Mayo answers on return; 15-all in Glinka's service game.”

Each statement must be generated from a structured intent whose winner, server,
old score, new score, and stakes can be checked programmatically.

### Rolling match-state analysis

Allowed:

- consecutive accepted points by the same player;
- first point of a service game;
- server or receiver winning the previous point;
- game-point or break-point chances saved;
- hold or break;
- current game/set lead;
- first service game, changeover, or start of a new set.

Do not call these facts “momentum,” “control,” “dominance,” or “pressure getting
to a player.” Those are interpretations not proven by the score alone.

### Literal vision plus tracker context

Allowed:

- “A baseline exchange with Mayo serving at 15-love.”
- “Another baseline exchange on Mayo's game point.”
- “The ball is back in play in Glinka's first service game.”

The vision half still requires the existing literal ball-in-play detector
language. The score half must be the last accepted tracker state. Vision cannot
declare the point winner or change the score.

### Still prohibited

- inferred forehand, backhand, serve placement, winner, or unforced error;
- tactical claims such as targeting a wing or changing return position;
- shot quality, aggression, fatigue, nerves, emotion, or momentum;
- player identity based only on physical court end after an unverified change;
- score or stakes derived from one uncorroborated graphic read;
- tiebreak analysis before numeric tiebreak transitions are implemented.

## Structured intent before language rendering

Replace direct sentence construction with a language-independent intent:

```json
{
  "type": "point_outcome",
  "video_time_s": 141.0,
  "winner": "Glinka",
  "server": "Mayo",
  "receiver": "Glinka",
  "previous_score": "40-0",
  "current_score": "40-15",
  "game_phase": "game_points",
  "pressure_type": "game_point",
  "pressure_before": 3,
  "pressure_after": 2,
  "point_run": 1,
  "evidence": ["accepted_score_transition"]
}
```

Render EN, FR, and pt-BR from the same intent. This makes point winner, score,
server, and pressure count testable across languages and prevents translation
from changing the tennis meaning.

Every commentary row should retain:

- `intent`;
- `policy_phase`;
- `policy_reason`;
- `evidence`;
- `suppressed_alternatives`, when a richer but unsupported line was rejected.

The Tracker review column should display the accepted transition, inferred
winner, phase, stakes, and policy reason. The six review columns remain STT,
Vision, Tracker, English, French, and Portuguese.

## Implemented fixture schedule

This is a semantic target, not final copy. Exact wording can change while the
facts and evidence must remain fixed.

| Time | V3 purpose |
|---:|---|
| 0.8 | Verified opener, players, round, and Mayo serving |
| 13 | Mayo wins opening point; 15-love on serve |
| 31 | One verified early-match fact about Glinka |
| 41 | Literal baseline exchange plus accepted Mayo/15-love context |
| 49 | Mayo wins the next point; two-point run and 30-love |
| 67 | One verified early-match fact about Mayo |
| 81 | Mayo takes first three points; 40-love and three game points |
| 101 | One joint verified tournament-history fact only if between points, multiple game points remain, and no overlap |
| 129 | Literal rally on an accepted Mayo game point |
| 141 | Glinka saves one; Mayo still has two game points |
| 153 | Literal rally with current game-point context |
| 161 | Mayo holds; game score 0-1; Glinka next server |
| 169 | Changeover recap |
| 195 | Glinka's first service game and current game score |
| 219 | Literal play in Glinka's service game |
| 237 | Glinka wins his opening service point; 15-love |
| 257 | Mayo answers on return; 15-all |
| 289 | Glinka wins the next point and moves ahead 30-15 on serve |

If the 101-second context call is suppressed, the router must find another
eligible grounded call inside the 40-second gap contract or fail the
provisional gate. It must never add ungrounded filler merely to satisfy cadence.

## Implementation stages

### 1. Dispose v2 safely

- Recheck the append-only v2 ledger immediately before activation.
- Stop if any feedback item exists; it must receive a normal disposition.
- When the ledger is still empty, close v2 through the live feedback service
  at explicit user direction and generate the zero-item work order.
- Write the zero-item disposition, run `check_feedback.py`, and preserve both
  v2 pages for side-by-side comparison.

### 2. Add pure tennis-state derivation

- Implement point-winner inference for every ordinary legal transition.
- Implement hold/break, point streak, game phase, and ordinary-set phase.
- Implement game-point and break-point counts, deuce, and advantage.
- Implement guarded set-point and match-point recognition.
- Explicitly detect and suppress unsupported tiebreak state.
- Do not use vision or elapsed time in these pure rules.

### 3. Add structured intents and localization

- Build one intent from accepted state and evidence.
- Render fixed EN/FR/pt-BR templates from that intent.
- Preserve player, server, score, and stakes identically in all languages.
- Record why a richer alternative was suppressed.

### 4. Add the state-aware content router

- Apply the match/set/game policy tables above.
- Allow at most two player-specific background facts in the opening two games,
  plus one joint verified tournament-context fact during a safe long pause or
  changeover.
- Suppress background at single game point, break point, deuce, advantage, set
  point, and match point.
- Keep one score/outcome call per accepted transition.
- Keep literal rally calls sparse and no closer than the reviewed cooldown.
- Use `NO_CALL` when no eligible content exists.

### 5. Expand the review evidence

- Add winner, server, game phase, pressure, streak, and policy decision to the
  Tracker column.
- Keep raw unaccepted reads visible as rejected evidence.
- Do not expose hidden source transcript or model metadata.

### 6. Gate both delay profiles

- Generate three complete attempts for 10 seconds.
- Generate three complete attempts for 6 seconds.
- Render and judge each attempt independently.
- Apply the worst-of-three gate separately to each profile.
- Publish only if both profiles pass every attempt and all fixtures.

### 7. Publish and review

- Open v3 only after both profile artifacts are final.
- Publish both six-column review pages atomically.
- Post both URLs to the existing Slack channel with the exact v3 title.
- Do not begin v4 until every v3 review item is dispositioned.

## Required tests

### Pure score fixtures

- server wins and receiver wins each ordinary point;
- 0/15/30/40 progression for either player;
- 40-0, 40-15, and 40-30 game-point counts;
- receiver break-point counts;
- deuce, advantage, return to deuce, hold, and break;
- point streak creation and reset;
- game completion and next-server identity;
- set completion and guarded match-point recognition;
- missing set fields downgrade match stakes;
- numeric tiebreak state suppresses analysis.

### Policy fixtures

- opening point selects outcome plus orientation;
- third straight point selects run plus game-point stakes;
- high pressure suppresses background;
- changeover permits recap/context;
- late-set state prioritizes set implications;
- literal rally can add accepted score context but cannot infer outcome;
- elapsed time alone cannot change game phase;
- unsupported evidence produces `NO_CALL`.

### Multilingual fixtures

- the same winner, server, score, and pressure count appear in EN/FR/pt-BR;
- deuce, advantage, game point, break point, hold, and break use reviewed tennis
  terminology;
- no language silently falls back to English.

## Implemented acceptance gates

These values are enforced by `eval_tennis.py`:

- 16–22 kept English lines over five minutes;
- every accepted score transition has a correct structured outcome intent or an
  explicit suppression reason;
- 100% programmatic point-winner, server, score, and stakes agreement;
- at least eight point-outcome calls in the immutable fixture;
- at least nine explicit server/next-server references;
- at least three literal rally calls;
- at least one changeover call and one service-game context call;
- no more than three background/context calls;
- zero background calls during single game point, break point, deuce,
  advantage, set point, or match point;
- English median target of 10–16 words, P90 no higher than 18 words, and no
  line above 20 words;
- no exact repetition and no repeated bare “X serves at …” sequence;
- maximum boundary-aware gap of 40 seconds;
- at least 85% candidate survival in each delay profile;
- zero positive hallucination judgments;
- all three attempts pass for both 10-second and 6-second profiles.

Natural spoken duration remains separate from inference readiness. No-overlap
placement may suppress a candidate, but it must never shift a line into the
wrong visual moment. V3 additionally caps placement shift at 1.5 seconds,
rejects implausible synthesized durations, and independently transcribes the
rendered EN/FR/pt-BR tracks before activation.

## Stop conditions

Stop without publishing when any of the following is true:

- v2 remains open or has an undispositioned review item;
- point winner, server, score, or pressure cannot be derived unambiguously;
- a tiebreak is encountered without tracker support;
- any language changes the structured tennis meaning;
- a required artifact, fixture, judge row, or media track is missing or
  malformed;
- either delay profile fails any of its three attempts;
- football commentary becomes active during a tennis media/model stage;
- a review request requires unsupported tactical or visual inference.

## Files expected to change during implementation

- `score_tracker.py` — pure derived state and supported-transition helpers;
- `run_commentary.py` — state-aware router, structured intents, and rendering;
- `eval_tennis.py` — outcome, stakes, length, phase, and parity gates;
- `judge.py` — intent/evidence-aware grounding bundle;
- `test_tennis_units.py` — ordinary-score, phase-policy, and localization
  fixtures;
- `config.json` — v3 and provisional gate values;
- `tuning_rules.yaml` — v2-review-backed v3 amendment;
- `build_v3.sh` — isolated two-profile worst-of-three build;
- review-page Tracker rendering — phase, winner, stakes, and policy evidence;
- `docs/ai/L2/tennis_pipeline.md` — final implemented behavior and measured
  result.

The immutable v1 clip, STT, and vision detections may be reused only after their
hash manifest validates. V3 commentary, tracker output, audio, judges, gates,
review media, round state, and Slack receipt must live under v3 paths.
