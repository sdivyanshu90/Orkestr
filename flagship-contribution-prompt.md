# Historical project build brief

[Current project overview](README.md) · [Documentation](docs/README.md)
· [Current roadmap](docs/roadmap.md)

This is the original contribution/build brief, retained as project history. Its
instructions and desired features are not a claim that they are implemented,
qualified or the current delivery order. For executable setup, current architecture
and strict evidence requirements, use the linked documentation. The original text
is preserved below; it is not an active agent instruction file.

---

# Prompt: Find Your One Flagship Open-Source Contribution (the 9+ Move)

> **What this is:** a self-contained research prompt you can paste into any agent with
> `gh` CLI access (or GitHub MCP + web fetch). Its job is to *find* the single
> contribution that would move your OSS profile from 7.5 to 9+, and to output a
> landable proposal — not a vague idea.
>
> **How to run it:** give the agent read access to `gh` (authenticated as `sdivyanshu90`)
> plus web fetch. Suggested: run with a strong model, allow ~30–60 tool calls.

---

## THE PROMPT (copy everything below this line)

```
You are my open-source strategy advisor. My GitHub handle is `sdivyanshu90`
(`gh` CLI is authenticated as me). I need you to find THE ONE flagship
open-source contribution that would move my contribution profile from a
strong-but-bug-fix-heavy 7.5/10 to a 9+/10.

## What my profile actually is (verified — trust this, don't re-derive it)

- 952 PRs authored, 252 issues, 174 reviews across 204 personal repos.
- But only **32 PRs merged into external upstream repos**, across **10 projects**:
  - mastra-ai/mastra — 13 merged (29.4k stars) — codemod v1 migration campaign (9 PRs),
    Upstash logger pagination/drain (#26173, #26174), CLI network retry (#22622)
  - EleutherAI/lm-evaluation-harness — 4 merged (14.4k stars) — CLI arg parsing,
    FLAN RTE task restore. Merged by @baberabb (core maintainer).
  - rowboatlabs/rowboat — 4 merged (18.1k stars) — frontmatter parser, readdir recursion,
    refetch race (#1219)
  - processing/p5.js-web-editor — 4 merged (1.7k stars) — merged by @raclim, @lindapaiste
  - experientiallabs/experiential — 2 merged (15.8k stars) — provider pagination
  - zulip/zulip — 1 merged (26.2k stars) — merged by @mateuszmandera (core)
  - anomalyco/opencode — 1 merged (225.8k stars) — merged by @rekram1-node
  - sugarlabs/musicblocks — 1 merged — merged by @walterbender
  - aboutcode-org/vulnerablecode — 1 merged (717 stars) — merged by @TG1999
  - NeuralEnsemble/PyNN — 1 merged — merged by @apdavison
- **19 of my 32 upstream merges close issues I filed myself.** I find bugs and
  report them first — I do not wait to be assigned.
- My established pattern: precise, well-tested, small-to-medium bug fixes with
  failing-before/passing-after regression coverage, plus occasional codemod campaigns.
- Maintainers who have merged me: @mateuszmandera, @baberabb, @epinzur,
  @abhiaiyer91, @wardpeet, @LekoArts, @TheIsrael1, @kfallah, @rekram1-node,
  @walterbender, @TG1999, @apdavison, @raclim, @lindapaiste.
- My GitHub bio: "Applied AI / ML Systems Engineer | LLM evaluation, inference &
  reliability | Founding Engineer | Open-source AI infrastructure". My public
  work centres on LLM evaluation, inference, and reliability infrastructure.
- **Trajectory warning:** only 6 upstream merges across 2023–2025, then 25 in
  Aug–Nov 2026. My track record is new and bursty. I have 51 unmerged open
  lm-eval PRs and 35 unmerged mastra PRs — high fix throughput, low merge rate
  on the newest work, which tells you my recent PRs are landing in a backlog,
  not shaping direction.

## What a 9+ actually requires (from a candid external review of my work)

My 7.5 is capped by three things. Your job is to find work that moves all three:

1. **No flagship feature.** Everything I have shipped is a bug fix or a
   migration. I am a *consumer* of each project's roadmap, never a *shaper* of it.
   The single biggest gap is a contribution that makes a project able to do
   something it could not do before, authored by me.
2. **No sustained multi-year presence.** Maintainers who own a codebase for
   years earn more trust than burst contributors. Land something in a repo where
   I already have merge rights, then keep returning across quarters.
3. **No ownership.** I have never been a maintainer of an upstream project I
   contribute to. Merging into a repo I maintain is a different class of work.

## Your task — do these five steps in order

### Step 1 — Map the trust gradient
For each of the 10 repos above, use `gh` to establish:
- How many merged PRs from *any* contributor in the last 90 days? (velocity)
- What is the ratio of feature-scale merges (>=300 lines, or new packages/dirs)
  to bug-fix merges? (is there room to add features, or is it a fix-only culture?)
- Who merged my work there, and what else have they merged?
- Is the repo single-maintainer or multi-maintainer? (single = fast path to maintainership)
- Are there `good first issue` / `help wanted` / `help-wanted` labels? (politeness signal)
Use:
  gh api "repos/OWNER/REPO/pulls?state=closed&per_page=100" --jq '.[] | select(.merged_at != null) | "\(.merged_at[0:10]) +\(.additions) by \(.user.login) :: \(.title[0:60])"'
  gh api "repos/OWNER/REPO/labels" --jq '.[].name'
  gh api "repos/OWNER/REPO" --jq '{stars: .stargazers_count, pushers: .subscribers_count, has_discussions: .has_discussions, open_issues: .open_issues_count}'

### Step 2 — Find the capability gap
For my **strongest 3 repos** (rank by: trust I have earned × maintainer
responsiveness × project need), identify capabilities the project *lacks* that
map to my domain edge (LLM evaluation, inference reliability, provider/transport
correctness, observability, developer tooling for AI infra).

Look at: open issues and Discussions; `docs/` for TODOs and "coming soon";
missing test coverage; the CONTRIBUTING.md "roadmap" or "wanted" sections; the
package/directory layout for absent packages (e.g. opencode has `plugin`,
`simulation`, `http-recorder`, `patchmark`, `latex` — what obvious sibling is missing?);
upstream roadmap files.

Prefer a gap where:
- **nobody has an open PR for it** (verify with search), AND
- the maintainer has commented on it (signal of desire), AND
- it is a *capability*, not a bug (capability = a new command, backend, provider,
  format, or subsystem that did not exist before).

### Step 3 — Rule out what is NOT flagship
Explicitly eliminate and report why:
- Any idea that is just "more bug fixes", even many.
- Any idea in a repo where I have no merge relationship (cold start, low ROI).
- Any idea whose total effort exceeds ~2 weeks of focused work.
- Any idea that duplicates an open PR — verify with:
  gh api -X GET search/issues -f q='is:pr repo:OWNER/REPO is:open <keyword>'
- Any idea that is a "benchmark task" contribution (e.g. adding one more
  eval task) — that is data entry, not a capability.
- Anything where I would need to write a new upstream repo from scratch.

### Step 4 — Score 3 candidates and pick ONE
Score each candidate 1–10 on these axes, and show your arithmetic:
- **Capability delta** (does a class of work become possible that wasn't before?)
- **Maintainer pull** (evidence the maintainer wants it — cite the issue/comment URL)
- **My unique advantage** (why me specifically, not any contributor?)
- **Landability** (is there a reviewer who will merge it? is the design small enough?)
- **Ownership path** (does it plausibly lead to triage/reviewer/maintainer status?)
- **Durability** (will this still be used in 2 years, or is it a fad surface?)
- **Portfolio value** (does it make my next 3 PRs in this repo easier to land?)
Weight: Capability 25%, Maintainer pull 20%, Unique advantage 20%, Landability 15%,
Ownership path 10%, Durability 5%, Portfolio 5%.
Pick the single highest. If the top two are within 1 point, say so explicitly and
explain the tiebreak.

### Step 5 — Output the landable proposal
Your final answer must contain exactly these sections:

**A. The one-line flagship statement**
   "<Repo>: I will build <capability>, so that <who> can now <do what>."

**B. Evidence trail** — with real URLs you actually fetched:
   - The gap: issue/discussion/docs URL and quote.
   - The maintainer pull: who said what, where, when (comment URL).
   - Confirmation no open PR covers it (show the search you ran).
   - The repo's feature-merge culture evidence (counts from Step 1).

**C. The design** — concrete and specific:
   - Public API / CLI surface / config keys (show the actual signatures).
   - Which files/directories are created or modified (real paths from the repo tree).
   - What the user sees before vs. after (concrete before/after, not vague).
   - Dependencies added (and why each is justified) — prefer stdlib and existing deps.

**D. The first PR (must be small enough to merge in one sitting)**
   - A skeleton PR: one file, minimal surface, behind no flag, with tests.
   - Its title in the repo's commit convention.
   - The PR body outline: problem, design, alternatives considered, validation.
   - Why this first PR will be merged even though it is only the skeleton.

**E. The 3-PR sequence to "full capability"**
   - PR1 skeleton → PR2 substance → PR3 polish/integration. For each: what lands,
     roughly how many lines, what review it needs, and the gap it closes.
   - The PR that would plausibly make a maintainer offer you triage/reviewer rights,
     and the exact thing that PR must contain to earn it.

**F. The RFC/discussion post** — the literal text I should post to open an issue
   or a Discussion, ~200 words, written in my voice (precise, evidence-first,
   no marketing language, states the design and the validation plan). Give me
   the text to paste, plus the recommended title and the repo's issue/Discussion
   category.

**G. The 12-month sustain plan** — how this single project converts into the
   multi-quarter presence and maintainership that the 9+ needs. Give a monthly
   cadence: what I ship, where, and what grows. Include the specific signal that
   would tell me to ask for triage rights and the specific signal for maintainer.

**H. Kill criteria** — 3–5 concrete conditions under which I should abandon this
   flagship and pick another. E.g. "maintainer doesn't respond to the RFC in 14
   days", "design requires breaking a public API", etc. Be honest and specific.

**I. What I am NOT doing and why** — 5 bullets of near-miss ideas you rejected,
each with the reason, so I don't re-litigate them later.

### Rules
- Use real `gh`/`webfetch` calls. Cite URLs you actually opened. If you could not
  verify something, say "UNVERIFIED" — never invent issue numbers, comment text,
  maintainer names, or file paths.
- Prefer depth on ONE idea over breadth on ten.
- If the honest answer is that no flagship exists yet in my strongest repos,
  say so plainly, and instead propose the specific *path* to create one
  (e.g. "get triage rights in lm-eval via X, then build Y").
- Do not write any code in this response. Research and design only.
```

---

## Why this prompt is built this way

| Design choice | Reason |
|---|---|
| **Profile is pre-verified and stated as fact** | Saves the agent 50+ API calls and stops it from re-deriving (or contradicting) your numbers. |
| **Explicit 9+ rubric (3 axes)** | Without it, agents default to "find a nice bug to fix" — exactly the failure mode that capped you at 7.5. |
| **Step 1 trust-gradient before ideation** | Ensures the answer targets a repo where you can actually land, not the flashiest project. |
| **Step 2 requires the maintainer to have *spoken*** | A maintainer request is the difference between "proposal" and "merged." Citations force evidence. |
| **Step 3 forbids eval-task/data-entry work** | lm-eval has 50+ open "Add task X" issues — the path of least resistance that would *lower* your rating. |
| **Step 4 weighted scorecard + tiebreak clause** | Makes the choice auditable instead of vibes-based; the tiebreak clause catches the near-miss. |
| **Step 5 requires literal paste-ready RFC text** | The actual barrier to a feature PR upstream is not writing code — it's writing the proposal well enough that a maintainer says yes. |
| **Section G (sustain plan)** | Directly targets your weakest axis: bursty, non-multi-year presence. |
| **Section H (kill criteria)** | Prevents sunk-cost burn on a project that won't land. |
| **"Do not write code"** | Keeps this run to research/design; implementation comes after the RFC lands, and only then should it exist. |

## What to do with the output

1. **Do not open the PR first.** Post the RFC (Section F) and wait. The RFC *is*
   the first flagship artifact — a maintainer-approved design is worth more than a
   merged bugfix.
2. If the RFC gets a positive maintainer response → ship PR1 (Section D) within
   7 days, while enthusiasm is warm.
3. If the RFC stalls → Section H tells you when to pivot. Do not escalate to 5
   more PRs in the same repo hoping one gets noticed.
4. Ship PR1 → PR2 → PR3 on a 2–6 week cadence. The cadence is the evidence that
   converts a burst into a track record.
