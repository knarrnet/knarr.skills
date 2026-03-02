# Concierge — Knarr Protocol Support Bundle

A portable Thrall recipe bundle that turns any knarr node into a protocol support interface. Three tiers at three depth levels. Drop it into your Thrall plugin directory, reload, and your node answers questions about the knarr protocol.

## What's Inside

```
concierge/
├── README.md              ← you are here
├── prompts/
│   ├── concierge-faq.toml       L0: FAQ triage (compact, fast LLM)
│   ├── concierge-expert.toml    L1: Expert consultation (full knowledge base)
│   └── concierge-intake.toml    L2: Deep engagement intake (collection, not answering)
├── recipes/
│   ├── concierge-faq.toml       L0: Free tier, classifies + answers FAQs
│   ├── concierge-expert.toml    L1: Priced tier, detailed protocol expertise
│   └── concierge-intake.toml    L2: Premium tier, structured intake for human review
└── rag/
    ├── 01-what-is-knarr.md      Protocol overview and philosophy
    ├── 02-getting-started.md    Installation and first steps
    ├── 03-skills.md             Skill development and registration
    ├── 04-mail.md               Node-to-node messaging
    ├── 05-economy.md            Bilateral credit system
    ├── 06-configuration.md      knarr.toml reference
    ├── 07-deployment.md         Docker, NAT, TLS, production setup
    └── 08-troubleshooting.md    Common issues and solutions
```

## Three Tiers

| Tier | Name | Price | What It Does |
|------|------|-------|-------------|
| L0 | FAQ / Triage | Free | Answers common questions, routes complex ones to L1 |
| L1 | Expert | Priced | Detailed answers from full knowledge base via serious LLM |
| L2 | Intake | Premium | Collects structured request for human expert review |

## Installation

### 1. Copy files into your Thrall plugin directory

```bash
# Copy prompts
cp concierge/prompts/*.toml /path/to/plugins/06-thrall/prompts/

# Copy recipes
cp concierge/recipes/*.toml /path/to/plugins/06-thrall/recipes/
```

### 2. Reload Thrall

```bash
touch thrall.reload
```

Or restart your node. Thrall will pick up the new recipes and prompts automatically.

### 3. Verify

Check your node logs for:
```
Recipe loaded: concierge-faq (mode=automated)
Recipe loaded: concierge-expert (mode=automated)
Recipe loaded: concierge-intake (mode=automated)
Loaded prompt: concierge-faq
Loaded prompt: concierge-expert
Loaded prompt: concierge-intake
```

## Wiring to Skills

The recipes define the pipeline behavior. To expose them as callable skills, create bridge skills that invoke `engine.run("concierge-faq", envelope)` (same pattern as `thrall-chat-lite`). Then register them in `knarr.toml`:

```toml
[skills.concierge-faq]
handler = "skills/concierge_faq.py:handle"
description = "Protocol FAQ — ask anything about knarr"
price = 0
visibility = "public"

[skills.concierge-expert]
handler = "skills/concierge_expert.py:handle"
description = "Expert consultation on knarr protocol and operations"
price = 3.0
visibility = "public"

[skills.concierge-intake]
handler = "skills/concierge_intake.py:handle"
description = "Submit a detailed support request for human review"
price = 8.0
visibility = "public"
```

## Expose (Storefront)

Add web forms for public access:

```toml
[expose.concierge-faq]
skill = "concierge-faq"
path = "ask"
mode = "static"
timeout = 15
display = { title = "Ask about Knarr", description = "Quick answers about the knarr protocol", result_format = "text" }
fields = { message = { label = "Your question", required = true } }

[expose.concierge-expert]
skill = "concierge-expert"
path = "consult"
mode = "static"
timeout = 30
display = { title = "Expert Consultation", description = "Detailed answers about protocol, deployment, and operations", result_format = "text" }
fields = { message = { label = "Your question", required = true } }
```

## Testing with thrall-inject

Before going live, test every recipe with `thrall-inject`:

```bash
curl -X POST http://localhost:8080/api/execute \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "skill": "thrall-inject",
    "input": {
      "body_text": "How do I register a skill?",
      "trigger_type": "on_mail",
      "dry_run": "true"
    }
  }'
```

The inject tool runs the question through the pipeline without side effects. Review the eval result, adjust prompts, repeat until the answers are good.

## Training Loop

1. Collect real questions (from GitHub issues, mail, user conversations)
2. Run them through `thrall-inject` with `dry_run: true`
3. Wrong answer? Adjust the prompt, add facts to the knowledge base, add hotwire patterns
4. Right answer? Pattern confirmed. Move to next question.
5. Use `thrall-tune` for stats on recipe activity and decision distribution

The concierge improves with every question. The recipes are the knowledge. The training data is real questions from real users.

## Backend Requirements

- **L0 (FAQ):** Any small LLM — gemma3:1b, qwen3:1.5b, or similar. CPU is fine.
- **L1 (Expert):** Larger LLM with big context window — Kimi (moonshot-v1-auto), Gemini Flash, qwen3:32b, or similar.
- **L2 (Intake):** Any LLM — the task is structured collection, not deep reasoning.

Configure your backend in `plugins/06-thrall/plugin.toml` under `[config.thrall]`.

## RAG Documents

The `rag/` directory contains the source knowledge base. These documents serve three purposes:

1. **Prompt material** — The expert prompt (L1) embeds key facts from these docs
2. **Operator reference** — Humans can read these docs directly for answers
3. **Future retrieval** — When Thrall adds retrieval-augmented generation, these become the corpus

## What This Is Not

- Not a chatbot. A receipted, priced, three-tier service.
- Not a spokesperson. Answers protocol questions. Does not represent any team.
- Not a monopoly. Any node can run its own concierge with its own knowledge and recipes.
