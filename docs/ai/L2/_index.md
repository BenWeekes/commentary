# Deep Dives Index

| Document | Summary | Load When |
|---|---|---|
| [blend_pipeline.md](blend_pipeline.md) | **AI live commentator — pipeline & process, current state (v6)**: signals, stages, profiles, guards/evals, feedback system, version history, open items | Working on `experiments/ai_commentator`; reviewing the pipeline or the HITL process |
| [hitl_tuning_workflow.md](hitl_tuning_workflow.md) | The improvement loop in depth: review → distill → implement → gate → accept; ledger state, process disciplines, round history | Distilling reviewer feedback into rules; running an acceptance gate |
| [review_cycle_1_dispositions.md](review_cycle_1_dispositions.md) | Per-comment audit of review cycle 1 (all 21 comments dispositioned with evidence) | Auditing how feedback was handled; writing the next dispositions doc |
| [resolution_tracker_eval.md](resolution_tracker_eval.md) | Resolution/tracker evaluation notes | Tracker/vision identity work |
| [tts_engine.md](tts_engine.md) | TTSEngine threading, buffer, pipe writer, atmosphere mixing | Modifying TTSEngine, debugging audio playback, changing buffer strategy |
| [stt_pipeline.md](stt_pipeline.md) | Deepgram → corrections → translation pipeline | Modifying Deepgram integration, adding corrections, changing translation flow |
| [tts_timeline_format.md](tts_timeline_format.md) | TTS playback log analysis and timing verification | Analysing TTS playback logs, debugging audio/video sync |
