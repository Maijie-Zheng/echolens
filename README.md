# EchoLens

English | [中文说明见 PRD](./PRD.md)

> Turn any video's comment section into an ingredient list anyone can read in 3 seconds.

**Status: early development.** This repo is being built in public. See the
[PRD](./PRD.md) for the product spec, architecture and roadmap.

## What it will do

Paste a video URL. CommentScope fetches the full comment section, classifies
every comment with [Jev](https://docs.typesafe.ai/) (a decision model that
returns calibrated probabilities instead of text), and renders a composition
chart — how much of your comment section is genuine praise, criticism,
suggestions, questions, memes, spam.

Built for creators who want to know what their audience is actually saying,
and for anyone curious what a comment section is really made of.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Comment fetching + local cache | not started |
| M1 | Jev classification + labeled baseline | not started |
| M2 | Composition report + CLI | not started |
| M3 | Advice engine + bilingual docs | not started |
| M4 | Public launch | not started |

## License

MIT
