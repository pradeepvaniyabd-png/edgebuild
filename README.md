# Super Agent X

Autonomous multi-agent assistant: intent → plan → task DAG → parallel specialist agents → verification → self-healing → synthesis.
Works on government documents (formal Hindi), SMC/ICT trading **analysis** (never places trades), data, code, and a full
creative pipeline (story → image → clip → voice → subtitles → FFmpeg edit → thumbnails → QC → approved export).

It boots with **no API keys**. Every missing capability is reported honestly instead of faked.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.11+
pip install -e ".[dev]"                           # add ,anthropic,bedrock,postgres as needed
cp .env.example .env                              # everything optional
super-agent doctor                                # what works, what is missing, how to fix it
super-agent run "system status dikhao"
super-agent create-video "chai wale ki kahani" --seconds 30 --language hinglish
super-agent serve                                 # API + dashboard on http://127.0.0.1:8765
```

Dashboard (optional): `cd frontend && npm install && npm run build`; `super-agent serve` then serves it at `/`.
Dev mode: `npm run dev` (proxies to port 8765).

## Commands

`run`, `resume`, `tasks`, `approvals`, `agents`, `tools`, `providers [--probe]`, `costs`, `logs`, `schedule`, `memory`, `db upgrade`,
`doctor`, `audit`, and creative: `factory` (topic → full content package), `create-image`, `create-video`, `storyboard`, `generate-scene`, `render`, `qc`, `generation-status`, `projects`.
Each supports `--help`.

## Autonomous video factory

```bash
super-agent factory "honesty" --category kids --language hinglish --seconds 30
super-agent run "content package on monsoon farming, category: education"
super-agent schedule add --name daily-kids --command "daily content: kids, honesty" --cron "0 7 * * *" --timezone Asia/Kolkata
```
Topic → idea → script → storyboard → characters → images → clips → voice → music → subtitles → thumbnail → metadata → QC.
Everything is prepared automatically; export (and any publishing) waits for your approval.

## Providers

Routing order: local → free → paid cloud primary → secondary → local fallback, with cooldowns on rate limits (limits are never bypassed).

| Capability | Adapters |
|---|---|
| Language | Ollama, OpenAI-compatible (OmniRoute, FreeLLMAPI, LM Studio, OpenAI), Anthropic, Bedrock |
| Image | ComfyUI, Replicate, fal, OpenAI |
| Video | ComfyUI, Replicate, fal; FFmpeg camera motion over stills (**not generative**, labelled as such) |
| Voice | Piper, espeak-ng, OpenAI TTS (edge-tts opt-in) |
| Embeddings | OpenAI, Ollama, built-in hashing |

## Safety model

- Path sandbox (workspace only, secrets files refused), command policy (allow / needs approval / deny, no shell), scrubbed child environment.
- Risky or paid actions wait in **Approvals**; export and publishing always need a human yes. Publishing is preparation only, nothing is uploaded.
- Secrets are masked in logs and API errors; memory refuses to store credentials. Auth token, rate limit, upload limits on the API.
- `super-agent audit` checks all of the above plus leaked secrets in the tree.

## Honest limits

- Without an LLM, planning summaries/stories use deterministic templates (the UI says so).
- Prompt alignment and character likeness are scored only when a vision-capable model is configured; otherwise reported as "not assessed".
- Generative image/video adapters are tested against a protocol double, not live vendors.
- Not suited to serverless hosting (SQLite, FFmpeg, long jobs). Use a VM/container.

## Development

```bash
pytest -q                       # SAX_TEST_POSTGRES_URL=postgresql+asyncpg://... also runs the PostgreSQL tests
ruff check src tests && mypy src
cd frontend && npm run build
```
Alembic migrations live in `src/superagent/db/migrations`; the server auto-migrates unless `SAX_AUTO_MIGRATE=false`.
