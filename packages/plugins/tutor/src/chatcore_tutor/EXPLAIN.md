+++
topic = "tutor"
title = "Language tutor preferences"
summary = "Saved profiles and topic settings guide model-generated practice and corrections."
aliases = ["language_tutor", "language_practice"]
+++

## Overview
The tutor combines saved language/level preferences with model-generated explanations, corrections
and practice. A profile can select explanation languages; group/topic settings can provide defaults.
This is a language-learning interface, not an independently verified grading engine.

## Workflow
The profile tool reads the requester's preferences and current topic status. Supported tutor
commands change personal or group/topic settings. The language-tutor skill applies practice guidance,
CEFR-appropriate wording, limited corrections and romanisation where applicable. Plain translation
is handled without automatically turning every language mention into a lesson.

## Storage
Tutor profiles and group/topic settings use the plugin's repository: Postgres when configured,
otherwise its in-memory implementation. They are separate from rolling conversation summaries.
Practice text follows the app's normal message/context storage. The plugin does not maintain a
complete verified learning record or a deterministic quiz score history merely by generating a lesson.

## Safety
The tool reads the requester's own profile. Group/topic changes require the applicable native role
checks; profile reads do not grant permission to change group settings. Auto-reply behavior depends
on the app's addressing policy and explicit topic configuration, not a passing language mention.

## Limitations
Grammar judgments, romanisation and translations are model-generated and can be incorrect.
Stored preferences can be missing or outdated. A configured profile does not guarantee every reply
matches the intended level, and framework support for auto-replies does not prove the app enables it.

## Verification
Read your profile/topic status with the supported tutor command and inspect target/explanation
languages and level. Ask for a correction with an explanation and compare uncertain points with
reliable language references. Change preferences through commands rather than assuming conversation
alone updated a setting.
