#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from score_tracker import (
    INITIAL,
    Score,
    ScoreTracker,
    is_legal_transition,
    point_winner,
    pressure_state,
    transition_type,
)
from run_commentary import (
    _score_from_detection,
    background_allowed,
    build_score_intent,
    court_mapping,
    generate_attempt,
    rally_call,
    render_score_intent,
    score_call,
    score_localizations,
)
from tennis_common import (
    OUTPUT_ARTIFACTS,
    PROFILES,
    PipelineError,
    SHARED_ARTIFACTS,
    read_jsonl,
    re_search_token,
)
from benchmark_tv_commentary import parse_vtt, window_metrics
from classify_tv_commentary import parse as parse_tv_classification
from build_tv_corpus import (
    CATEGORIES as CORPUS_CATEGORIES,
    FUNCTIONS as CORPUS_FUNCTIONS,
    has_four_word_overlap,
    load_sources as load_tv_corpus_sources,
    parse_classification as parse_corpus_classification,
    semantic_metrics as tv_corpus_semantic_metrics,
)
from build_review_page import install_media
from feedback_server import clean_text, digest
from judge import parse as parse_judge
from announce_slack import page_has_title
from render_tracks import (
    VOICES,
    placement_start,
    plausible_tts,
    tts_duration_limit_s,
    write_wave,
)
from merge_stt import normalized, rejection_reason
from collections import Counter


class ScoreTransitionTests(unittest.TestCase):
    def test_first_point_for_either_player(self):
        self.assertTrue(is_legal_transition(INITIAL, Score(0, 0, "15", 0, 0, "0", "near")))
        self.assertTrue(is_legal_transition(INITIAL, Score(0, 0, "0", 0, 0, "15", "near")))

    def test_server_cannot_change_mid_game(self):
        self.assertFalse(is_legal_transition(INITIAL, Score(0, 0, "15", 0, 0, "0", "far")))

    def test_skipped_point_is_rejected(self):
        self.assertFalse(is_legal_transition(INITIAL, Score(0, 0, "30", 0, 0, "0", "near")))

    def test_deuce_advantage_and_back(self):
        deuce = Score(0, 3, "40", 0, 2, "40", "far")
        advantage = Score(0, 3, "AD", 0, 2, "40", "far")
        self.assertTrue(is_legal_transition(deuce, advantage))
        self.assertTrue(is_legal_transition(advantage, deuce))

    def test_advantage_can_complete_game_for_either_player(self):
        far_advantage = Score(0, 3, "AD", 0, 2, "40", "far")
        near_advantage = Score(0, 3, "40", 0, 2, "AD", "far")
        self.assertTrue(
            is_legal_transition(far_advantage, Score(0, 4, "0", 0, 2, "0", "near"))
        )
        self.assertTrue(
            is_legal_transition(near_advantage, Score(0, 3, "0", 0, 3, "0", "near"))
        )

    def test_game_completion_changes_server(self):
        old = Score(0, 0, "40", 0, 0, "30", "near")
        self.assertTrue(is_legal_transition(old, Score(0, 1, "0", 0, 0, "0", "far")))
        self.assertFalse(is_legal_transition(old, Score(0, 1, "0", 0, 0, "0", "near")))

    def test_impossible_set_is_rejected(self):
        old = Score(0, 5, "40", 0, 4, "30", "near")
        self.assertFalse(is_legal_transition(old, Score(1, 0, "0", 0, 0, "0", "far")))

    def test_set_display_reset_does_not_flip_server_twice(self):
        old = Score(0, 7, "0", 0, 5, "0", "far")
        self.assertTrue(is_legal_transition(old, Score(1, 0, "0", 0, 0, "0", "far")))
        self.assertFalse(is_legal_transition(old, Score(1, 0, "0", 0, 0, "0", "near")))

    def test_point_winner_comes_from_legal_transition(self):
        self.assertEqual(
            point_winner(INITIAL, Score(0, 0, "0", 0, 0, "15", "near")),
            "near",
        )
        self.assertEqual(
            point_winner(
                Score(0, 0, "15", 0, 0, "40", "near"),
                Score(0, 0, "0", 0, 1, "0", "far"),
            ),
            "near",
        )

    def test_set_reset_is_not_counted_as_an_extra_point(self):
        old = Score(0, 7, "0", 0, 5, "0", "far")
        new = Score(1, 0, "0", 0, 0, "0", "far")
        self.assertEqual(transition_type(old, new), "set_reset")
        self.assertIsNone(point_winner(old, new))

    def test_pressure_distinguishes_game_break_set_and_match_points(self):
        game = pressure_state(Score(0, 2, "40", 0, 2, "15", "far"))
        self.assertEqual((game["owner"], game["count"]), ("far", 2))
        self.assertFalse(game["break_point"])

        break_point = pressure_state(Score(0, 2, "15", 0, 2, "40", "far"))
        self.assertTrue(break_point["break_point"])
        self.assertEqual(break_point["owner"], "near")

        set_point = pressure_state(Score(0, 5, "40", 0, 4, "30", "far"))
        self.assertTrue(set_point["set_point"])
        self.assertFalse(set_point["match_point"])

        match_point = pressure_state(Score(1, 5, "40", 0, 4, "30", "far"))
        self.assertTrue(match_point["match_point"])

    def test_tiebreak_state_is_explicitly_unsupported(self):
        state = pressure_state(Score(0, 6, "0", 0, 6, "0", "far"))
        self.assertFalse(state["supported"])


class CorroborationTests(unittest.TestCase):
    def test_change_requires_two_sightings(self):
        tracker = ScoreTracker()
        next_score = Score(0, 0, "0", 0, 0, "15", "near")
        first = tracker.observe(next_score, 0.95)
        second = tracker.observe(next_score, 0.95)
        self.assertFalse(first["accepted"])
        self.assertEqual(first["reason"], "awaiting_corroboration")
        self.assertTrue(second["accepted"])
        self.assertTrue(second["changed"])

    def test_uncertain_read_resets_candidate(self):
        tracker = ScoreTracker()
        next_score = Score(0, 0, "0", 0, 0, "15", "near")
        tracker.observe(next_score, 0.95)
        tracker.observe(None, 0.2)
        result = tracker.observe(next_score, 0.95)
        self.assertEqual(result["reason"], "awaiting_corroboration")

    def test_illegal_candidate_never_commits(self):
        tracker = ScoreTracker()
        bad = Score(0, 0, "40", 0, 0, "0", "near")
        self.assertEqual(tracker.observe(bad, 0.99)["reason"], "illegal_transition")
        self.assertEqual(tracker.observe(bad, 0.99)["reason"], "illegal_transition")
        self.assertEqual(tracker.current, INITIAL)


class PartialScoreReadTests(unittest.TestCase):
    def test_missing_sets_carry_forward(self):
        candidate = _score_from_detection(
            {
                "scoreboard": {
                    "visible": True,
                    "far_sets": None,
                    "far_games": 0,
                    "far_points": "0",
                    "near_sets": None,
                    "near_games": 0,
                    "near_points": "15",
                    "server": "unknown",
                }
            },
            INITIAL,
        )
        self.assertEqual(candidate, Score(0, 0, "0", 0, 0, "15", "near"))

    def test_visible_game_change_resets_points_and_flips_server(self):
        current = Score(0, 0, "15", 0, 0, "40", "near")
        candidate = _score_from_detection(
            {
                "scoreboard": {
                    "visible": True,
                    "far_sets": None,
                    "far_games": 0,
                    "far_points": None,
                    "near_sets": None,
                    "near_games": 1,
                    "near_points": None,
                    "server": "unknown",
                }
            },
            current,
        )
        self.assertEqual(candidate, Score(0, 0, "0", 0, 1, "0", "far"))

    def test_raw_server_guess_cannot_flip_mid_game(self):
        candidate = _score_from_detection(
            {
                "scoreboard": {
                    "visible": True,
                    "far_sets": None,
                    "far_games": 0,
                    "far_points": "15",
                    "near_sets": None,
                    "near_games": 0,
                    "near_points": "0",
                    "server": "far",
                }
            },
            INITIAL,
        )
        self.assertEqual(candidate.server, "near")

    def test_court_mapping_flips_after_first_game(self):
        self.assertEqual(court_mapping(INITIAL)["far_end"], "Daniil Glinka")
        after_one = Score(0, 0, "0", 0, 1, "0", "far")
        self.assertEqual(court_mapping(after_one)["near_end"], "Daniil Glinka")

    def test_court_mapping_flips_back_after_third_game(self):
        after_three = Score(0, 1, "0", 0, 2, "0", "near")
        self.assertEqual(court_mapping(after_three)["far_end"], "Daniil Glinka")

    def test_score_call_names_server_without_reversing_display_order(self):
        current = Score(0, 0, "15", 0, 0, "40", "near")
        self.assertEqual(
            score_call(INITIAL, current),
            "Mayo serves at 40-15 — two game points.",
        )

    def test_score_call_names_next_server_after_game(self):
        previous = Score(0, 0, "15", 0, 0, "40", "near")
        current = Score(0, 0, "0", 0, 1, "0", "far")
        self.assertEqual(
            score_call(previous, current),
            "Mayo holds in the opening game. Glinka will serve at 0-1.",
        )

    def test_score_localizations_preserve_leader_and_score(self):
        current = Score(0, 0, "15", 0, 0, "40", "near")
        self.assertEqual(
            score_localizations(INITIAL, current),
            (
                "Mayo sert à 40-15 — deux balles de jeu.",
                "Mayo saca em 40 a 15 — dois pontos para fechar o game.",
            ),
        )

    def test_score_localizations_preserve_next_server(self):
        previous = Score(0, 0, "15", 0, 0, "40", "near")
        current = Score(0, 0, "0", 0, 1, "0", "far")
        self.assertEqual(
            score_localizations(previous, current),
            (
                "Mayo tient son service dans le premier jeu. Glinka servira à 0-1.",
                "Mayo confirma o saque no game inicial. Glinka saca em 0 a 1.",
            ),
        )

    def test_advantage_names_player(self):
        current = Score(0, 0, "AD", 0, 0, "40", "near")
        self.assertEqual(
            score_call(INITIAL, current),
            "Advantage Glinka on Mayo's serve.",
        )
        self.assertEqual(
            score_localizations(INITIAL, current),
            (
                "Avantage Glinka sur le service de Mayo.",
                "Vantagem de Glinka no saque de Mayo.",
            ),
        )


class CommentaryCadenceTests(unittest.TestCase):
    def test_rally_call_fails_closed_after_human_review(self):
        self.assertIsNone(
            rally_call(
                "Far-end player strikes an overhead-like shot near the baseline.",
                0,
            )
        )
        self.assertIsNone(
            rally_call("Ball in play across the net.", 0)
        )

    def test_clip_fixture_has_server_led_tv_cadence(self):
        detections = read_jsonl(SHARED_ARTIFACTS / "detections.jsonl")
        stt = read_jsonl(SHARED_ARTIFACTS / "stt_merged.jsonl")
        fast_scores = read_jsonl(OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl")
        rows, tracker = generate_attempt(
            detections, stt, 1, fast_scores=fast_scores
        )
        self.assertEqual(len(tracker), 158)
        self.assertEqual(len(rows), 14)
        self.assertFalse([row for row in rows if row["src"] == "vision_rally"])
        self.assertEqual(
            len([row for row in rows if row["src"] == "score_tracker"]),
            8,
        )
        self.assertEqual(
            [row["video_time_s"] for row in rows if row["src"] == "score_tracker"],
            [12.4, 47.8, 80.6, 135.8, 160.2, 234.8, 255.0, 287.8],
        )
        self.assertEqual(
            [
                row["intent"]["code"]
                for row in rows
                if row["src"] == "score_tracker"
            ],
            [
                "opening_point",
                "server_run",
                "game_points",
                "game_point_saved",
                "hold",
                "opening_point",
                "receiver_answers",
                "server_ahead",
            ],
        )
        self.assertEqual(
            [
                row["text"]
                for row in rows
                if row["src"] == "score_tracker"
            ][3],
            "Glinka saves one, but Mayo still has two game points on serve at 40-15.",
        )
        self.assertTrue(all(row.get("intent") and row.get("policy") for row in rows))
        self.assertTrue(
            all(
                "local_fixed_layout_scoreboard_two_frame_confirmation"
                in row["intent"]["evidence"]
                for row in rows
                if row["src"] == "score_tracker"
            )
        )
        self.assertTrue(
            all(
                "serv" in row["text"].lower()
                for row in rows
                if row["src"] == "score_tracker"
            )
        )
        times = [0.0, *(float(row["video_time_s"]) for row in rows), 300.0]
        self.assertLessEqual(
            max(later - earlier for earlier, later in zip(times, times[1:])),
            40.0,
        )

    def test_two_second_profile_vetoes_background_near_score_change(self):
        detections = read_jsonl(SHARED_ARTIFACTS / "detections.jsonl")
        stt = read_jsonl(SHARED_ARTIFACTS / "stt_merged.jsonl")
        fast_scores = read_jsonl(OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl")
        with patch("run_commentary.DELAY_S", 2.0):
            rows, _tracker = generate_attempt(
                detections, stt, 1, fast_scores=fast_scores
            )
        self.assertEqual(len(rows), 14)
        self.assertNotIn(
            45.0,
            [row["video_time_s"] for row in rows if row["src"] == "pre_match_color"],
        )
        score_times = [
            row["video_time_s"] for row in rows if row["src"] == "score_tracker"
        ]
        self.assertTrue(
            all(
                not any(0 < score_time - row["video_time_s"] < 8.0 for score_time in score_times)
                for row in rows
                if row["src"] == "pre_match_color"
            )
        )

    def test_background_is_suppressed_at_single_or_break_point(self):
        allowed, _ = background_allowed(
            Score(0, 0, "30", 0, 0, "40", "near")
        )
        self.assertFalse(allowed)
        allowed, _ = background_allowed(
            Score(0, 0, "40", 0, 0, "15", "near")
        )
        self.assertFalse(allowed)

    def test_structured_intent_renders_the_saved_game_point(self):
        old = Score(0, 0, "0", 0, 0, "40", "near")
        new = Score(0, 0, "15", 0, 0, "40", "near")
        intent = build_score_intent(old, new, ["near", "near", "near", "far"])
        self.assertEqual(intent["code"], "game_point_saved")
        self.assertEqual(
            render_score_intent(intent)[0],
            "Glinka saves one, but Mayo still has two game points on serve at 40-15.",
        )

    def test_structured_intent_distinguishes_break_points(self):
        old = Score(0, 0, "0", 0, 0, "30", "far")
        new = Score(0, 0, "0", 0, 0, "40", "far")
        intent = build_score_intent(old, new, ["near", "near", "near"])
        self.assertEqual(intent["code"], "break_points")
        self.assertEqual(
            render_score_intent(intent)[0],
            "Mayo takes the first three points on return — "
            "three break points at love-40.",
        )


class ArtifactTests(unittest.TestCase):
    def test_jsonl_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.jsonl"
            path.write_text('{"ok": true}\nnot json\n')
            with self.assertRaises(PipelineError):
                read_jsonl(path)

    def test_process_token_match_does_not_accept_substrings(self):
        self.assertTrue(re_search_token("/env/python run_blend_live.py", "run_blend_live.py"))
        self.assertFalse(re_search_token("watch_run_blend_live.py_backup", "run_blend_live.py"))

    def test_feedback_text_replaces_lone_surrogate(self):
        cleaned = clean_text("broken \ud83d comment", 100)
        cleaned.encode("utf-8")
        self.assertIn("?", cleaned)

    def test_feedback_digest_fails_on_malformed_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            feedback = Path(folder)
            version = feedback / "v1"
            version.mkdir()
            (version / "comments.jsonl").write_text('{"items":[]}\nnot-json\n')
            with patch("feedback_server.FEEDBACK", feedback):
                with self.assertRaises(ValueError):
                    digest("v1")

    def test_stt_rejects_prompt_echo(self):
        row = {
            "text": "Transcribe only audible speech, including umpire score calls.",
            "provider": "whisper",
            "conf": 0.9,
        }
        self.assertEqual(
            rejection_reason(row, Counter({normalized(row["text"]): 1})),
            "prompt_echo",
        )

    def test_stt_rejects_pathological_repetition(self):
        row = {"text": "Oh.", "provider": "whisper", "conf": 0.9}
        self.assertEqual(
            rejection_reason(row, Counter({"oh": 261})),
            "pathological_repetition",
        )

    def test_judge_preserves_numeric_positive(self):
        result = parse_judge(
            '[{"index":0,"hallucination_likely":1,"reason":"unsupported"}]',
            1,
        )
        self.assertEqual(result[0]["hallucination_likely"], 1)

    def test_judge_rejects_boolean_schema(self):
        with self.assertRaises(ValueError):
            parse_judge(
                '[{"index":0,"hallucination_likely":false,"reason":"no"}]',
                1,
            )

    def test_judge_rejects_missing_index(self):
        with self.assertRaises(ValueError):
            parse_judge(
                '[{"hallucination_likely":0,"reason":"no"}]',
                1,
            )

    def test_ready_audio_duration_is_not_inference_latency(self):
        # A spoken clip prewarmed before the match is valid in a 300-second
        # delayed output; the profile deadline was already checked on readiness.
        self.assertEqual(
            placement_start(
                desired=17,
                previous_end=0,
                audio_size=60,
                output_size=3000,
            ),
            17,
        )

    def test_audio_cannot_overrun_output_buffer(self):
        self.assertIsNone(
            placement_start(
                desired=2950,
                previous_end=0,
                audio_size=100,
                output_size=3000,
            )
        )

    def test_tts_duration_guard_rejects_provider_audio_outlier(self):
        text = "A normal short commentary sentence for the opening point."
        self.assertLess(tts_duration_limit_s(text), 12.0)
        self.assertTrue(plausible_tts(text, b"\0" * 5 * 16000 * 2))
        self.assertFalse(plausible_tts(text, b"\0" * 30 * 16000 * 2))

    def test_slack_page_marker_decodes_required_utf8_title(self):
        title = "AI Tennis commentator — v4 ready for review"
        self.assertTrue(page_has_title(title.encode("utf-8"), title))
        self.assertFalse(page_has_title(b"\xff", title))

    def test_tennis_english_voice_is_pinned(self):
        self.assertEqual(VOICES["en"], "kfU9VUUMjY4PWNoUfZ45")

    def test_v4_profiles_are_five_and_two_seconds(self):
        self.assertEqual(PROFILES, ("5s", "2s"))

    def test_atomic_render_breaks_deployment_hard_link(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            deployed = Path(folder) / "deployed.wav"
            source.write_bytes(b"previous-review-media")
            install_media(source, deployed)
            self.assertEqual(source.stat().st_ino, deployed.stat().st_ino)
            write_wave(source, b"\0" * 320)
            self.assertNotEqual(source.stat().st_ino, deployed.stat().st_ino)
            self.assertEqual(deployed.read_bytes(), b"previous-review-media")

    def test_tv_benchmark_parses_cues_and_measures_window(self):
        cues = parse_vtt(
            "WEBVTT\n\n"
            "00:00.000 --> 00:02.000\nFirst turn.\n\n"
            "00:04.000 --> 00:06.000\nSecond turn.\n"
        )
        result = window_metrics(
            cues,
            {"label": "fixture", "start_s": 0.0, "end_s": 10.0},
        )
        self.assertEqual(result["caption_cues"], 2)
        self.assertEqual(result["merged_speech_turns"], 2)
        self.assertEqual(result["maximum_caption_silence_s"], 4.0)

    def test_tv_semantic_classifier_schema_is_fail_closed(self):
        parsed = parse_tv_classification(
            '[{"index":0,"category":"tactics_or_pattern","reason":"strategy"}]',
            1,
        )
        self.assertEqual(parsed[0]["category"], "tactics_or_pattern")
        with self.assertRaises(ValueError):
            parse_tv_classification(
                '[{"index":0,"category":"invented","reason":"bad"}]',
                1,
            )

    def test_tv_corpus_manifest_uses_fixed_official_windows(self):
        catalog = load_tv_corpus_sources()
        self.assertEqual(len(catalog["sources"]), 5)
        for source in catalog["sources"]:
            self.assertTrue(source["page"].startswith("https://www.wtatennis.com/"))
            self.assertIn(
                source["benchmark_role"],
                {"commentary_reference", "world_feed_control"},
            )
            self.assertEqual(len(source["windows"]), 3)
            self.assertTrue(
                all(
                    row["end_s"] - row["start_s"] == 300
                    for row in source["windows"]
                )
            )

    def test_tv_corpus_paraphrase_overlap_guard(self):
        self.assertTrue(
            has_four_word_overlap(
                "She is stepping inside the baseline on second serves.",
                "She is stepping inside to pressure the return.",
            )
        )
        self.assertFalse(
            has_four_word_overlap(
                "She is stepping inside the baseline on second serves.",
                "Describes aggressive return positioning.",
            )
        )

    def test_tv_corpus_classification_schema_and_copy_guard(self):
        turns = [
            {
                "start_s": 10.0,
                "end_s": 13.0,
                "text": "She is stepping inside the baseline on second serves.",
            }
        ]
        category = CORPUS_CATEGORIES[2]
        function = CORPUS_FUNCTIONS[6]
        parsed = parse_corpus_classification(
            json.dumps(
                [
                    {
                        "index": 0,
                        "category": category,
                        "function": function,
                        "paraphrase": "Describes aggressive return positioning.",
                    }
                ]
            ),
            turns,
        )
        self.assertEqual(parsed[0]["category"], category)
        guarded = parse_corpus_classification(
            json.dumps(
                [
                    {
                        "index": 0,
                        "category": category,
                        "function": function,
                        "paraphrase": "She is stepping inside to pressure returns.",
                    }
                ]
            ),
            turns,
        )
        self.assertEqual(
            guarded[0]["paraphrase"],
            "Explains a tactical or technical detail.",
        )
        self.assertEqual(
            guarded[0]["paraphrase_guard_status"],
            "safe_fallback_source_overlap",
        )

    def test_tv_corpus_gap_metrics_do_not_cross_sample_windows(self):
        rows = [
            {
                "start_s": 10,
                "end_s": 12,
                "word_count": 5,
                "category": "score_or_server",
                "function": "states_score_or_server",
                "window": "early",
            },
            {
                "start_s": 20,
                "end_s": 22,
                "word_count": 5,
                "category": "score_or_server",
                "function": "states_score_or_server",
                "window": "early",
            },
            {
                "start_s": 1800,
                "end_s": 1802,
                "word_count": 5,
                "category": "score_or_server",
                "function": "states_score_or_server",
                "window": "middle",
            },
        ]
        metrics = tv_corpus_semantic_metrics(rows, 10)
        self.assertEqual(metrics["maximum_interturn_gap_s"], 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
