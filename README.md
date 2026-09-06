# JobHunter

**English** | [简体中文](README.zh-CN.md)

A local job-search and application tracker for the Australian market. JobHunter brings together roles from **SEEK, Indeed and LinkedIn**, removes duplicates, ranks them against your candidate profile, and keeps an auditable application history.

JobHunter handles discovery, scoring and the Dashboard. The separately installed **`applypilot-au` Skill** handles platform policy and permitted browser application workflows.

## Features

- **Multi-source discovery:** SEEK integration plus JobSpy for Indeed and LinkedIn, with normalised job fields and annual AUD salaries.
- **Duplicate detection:** URL and normalised company/title/location matching, followed by fuzzy title matching; alternative source links are retained.
- **Explainable matching:** LLM job-fit scores, summaries, strengths, gaps and sponsorship signals, alongside a separate resume-fit score and selection reason.
- **Candidate-driven screening:** configurable work-rights rules, entry/junior eligibility, and application priority for jobs posted within 24 hours, then 3 days.
- **Persistent Dashboard:** job cards, filters, application status changes, selected resumes and an append-only event history.
- **Optional application materials:** resume tailoring and Australian-style cover letters based on existing candidate facts.
- **Local web controls:** edit settings and candidate details, review scoring rules, run searches, and configure macOS scheduled searches.

## Quick start

The commands below use a macOS/Linux shell and a Python virtual environment. Scheduled searches use macOS LaunchAgents. Browser application workflows require Codex and the separately installed ApplyPilot Skill.

### 1. Install dependencies

```bash
git clone https://github.com/ispengwang/jobHunter.git
cd jobHunter
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. Prepare your profile and configuration

Personal profile files are excluded from Git and must be created locally:

| File | Purpose |
| --- | --- |
| `profile/resume.md` | Your factual source resume: experience, education, skills and projects. |
| `profile/preferences.md` | Target roles, locations, salary preferences and screening constraints. |
| `profile/candidate_profile.md` | Editable candidate facts, initialised from the resume once. Complete and review these in the web app. |
| `profile/resumes/manifest.yaml` | Resume variant metadata; update its entries and paths for your own files. |

Review `config.yaml` before running. Set your search terms, locations, sources, work-rights filters, matching thresholds and file paths. The checked-in configuration needs adapting to your environment.

**Scoring also requires a separate local installation of `applypilot-au`.** This Skill is not bundled in this repository. Set `applypilot.skill_path` to its installed directory; it must contain `references/deepseek-scoring-rules.md`. This dependency applies to ordinary scoring as well as autopilot. Scrape-only runs do not require an LLM key or scoring rules.

For the DeepSeek workflow, update these fields in the existing configuration, keeping the other settings:

```yaml
llm:
  provider: "deepseek"
  # Set deepseek_model to a model available to your API account.

applypilot:
  skill_path: "/absolute/path/to/applypilot-au"
```

Export the API key in the shell used to launch JobHunter:

```bash
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
```

Ordinary scoring also supports Anthropic (`ANTHROPIC_API_KEY`) and OpenAI (`OPENAI_API_KEY`) through the corresponding `llm.provider` and model settings. **`--autopilot` requires DeepSeek.**

### 3. Run a small search

```bash
# Fetch jobs without LLM calls.
python run.py --scrape-only

# Score up to 20 jobs from the cached results.
python run.py --from-cache --limit 20

# Run discovery, scoring and Dashboard synchronisation.
python run.py
```

Read `output/jobs-ranked.md` for job summaries and match explanations, or use `output/jobs-ranked.csv` for spreadsheet filtering. The ranked exports are built from the persistent Dashboard, so they can include jobs from earlier runs.

Existing Dashboard jobs are normally not scored again. After changing your profile or scoring rules, use `python run.py --from-cache --rescore-all` to explicitly rescore cached jobs; this makes new LLM calls.

## Local web app

```bash
python webapp.py
```

Open [the local app](http://127.0.0.1:5050). It listens on `127.0.0.1`.

| Page | What you can do |
| --- | --- |
| `/` | Edit settings, launch searches, inspect logs and configure schedules. |
| `/profile` | Review and edit candidate details without overwriting them on later resume imports. |
| `/dashboard` | Filter by match score, source, freshness, status and daily activity; inspect jobs and update application progress. |
| `/scoring-rules` | Review or edit the installed Skill's scoring rules. Manual saves back up the previous version. |

The Dashboard button adds a job to the local application list; it does not start a background executor or open a recruitment site. The current ApplyPilot Agent reads these records with `python run.py --handoff-list` and continues under the Skill's rules. After successfully submitting on an external platform yourself, use the card's submission confirmation action to record that outcome.

Saving schedule settings only saves the configuration. Use the separate **save and apply to macOS scheduled tasks** action to update LaunchAgents. Scheduled processes do not inherit your interactive shell's exported API keys; see the scheduling setup in [HOWTO.md](HOWTO.md) (Chinese).

## Application workflow

```text
Discover → Normalise → Deduplicate → Hard filters → LLM scoring
                                                        ↓
                              Eligibility + freshness + resume selection
                                                        ↓
                                   Dashboard + ranked exports + history
                                                        ↓
                                  Optional materials → ApplyPilot → Outcome
```

Job-fit and resume-fit are separate decisions. Eligible roles are assigned a `broad` or `targeted` preparation path; other roles remain for `manual_review`. Application priority uses freshness before score within each freshness group. Dashboard ranking remains score-based.

### Generate materials

```bash
python run.py --from-cache --generate
```

Generation uses `scoring.generate_threshold` and `scoring.max_generate`, and prepares eligible `broad`/`targeted` roles from the current scoring run. If cached jobs are already in the Dashboard, add `--rescore-all` to rescore and generate for them.

Each generated application directory contains `00-summary.md`, `resume.md` and `cover-letter.md`. Review every document before use: tailoring may reorganise or rephrase existing facts, but must not invent experience, skills, dates or achievements.

### Continue with ApplyPilot

In Codex, ask the current Agent to use `applypilot-au` for the search-and-apply workflow. Its pipeline entry point is:

```bash
python run.py --autopilot
```

When new jobs reach the scoring stage, this mode also prepares materials and writes `output/agent-run.json` for the current Agent to consume and continue eligible attempts. Running the command alone does not perform browser submissions. A run with no jobs to score returns early.

- LinkedIn, Indeed and SEEK's own site default to manual submission in the current integration.
- Direct employer ATS flows can use `auto_if_allowed` only with current permission and all platform and candidate checks satisfied.
- CAPTCHA, login/2FA, unknown mandatory answers, legal terms and unsupported forms require user intervention.
- An internal selection or generated document is not a submission. `submitted` requires external success evidence or the user's explicit confirmation of a successful external submission.

The internal attempt ledger supports deduplication, recovery and audit; it is not a user-operated queue. Full-run and single-job workflows share daily limits. See [the browser application runbook](BROWSER_APPLICATION_RUNBOOK.md) (Chinese) for platform modes, limits and CLI outcome updates. Its description of a Dashboard background executor is outdated; the current button only creates a local handoff record.

## Files and persistence

```text
scrapers/                         SEEK and JobSpy source adapters
run.py                            Pipeline and attempt-management CLI
webapp.py                         Local settings and Dashboard server
config.yaml                       Search, LLM, application and path settings
profile/                          Local candidate facts and resume variants
data/
  application-dashboard.csv       Persistent job/application ledger
  application-events.csv          Append-only event history
  application-attempts.csv         Internal Agent execution ledger
output/
  jobs-raw.csv                     Deduplicated discovery results
  jobs-ranked.csv                 Ranked Dashboard export
  jobs-ranked.md                  Readable ranked summaries
  rejected-visa.csv               Jobs excluded by work-rights filters
  agent-run.json                  Agent continuation manifest
  applications/                   Generated application materials
```

`data/` holds application memory; keep it when refreshing exports. Personal profile files, resume Markdown files, `data/`, `output/` and `.env` are ignored by Git. `config.yaml` and the resume manifest are tracked, so keep secrets and private candidate details out of them.

Storage is local, but LLM scoring and generation send resume/job content to the configured API provider. Browser applications transmit candidate data to the selected recruitment service when authorised.

## Verification

Run the offline regression suites from the project root:

```bash
python test_pipeline.py
python test_seek_parse.py
python test_e2e_offline.py
python test_application_system.py
```

These cover normalisation, deduplication, work-rights filtering, SEEK parsing, the pipeline with stubbed LLM responses, candidate profiles, application policy and Dashboard behaviour. They do not establish that live recruitment endpoints or submissions currently work.

## Known limitations

- SEEK uses non-public endpoints that may change. If full job descriptions cannot be retrieved, the adapter can fall back to teaser text and bullet points, reducing scoring context.
- Job-source availability and rate limits vary. Respect platform restrictions; do not bypass CAPTCHA or anti-bot controls.
- Company-name differences and sparse listings can still cause missed duplicates or uncertain matches.
- Sponsorship signals, generated summaries and fit scores need review. A fit score is not an interview probability or a determination of work rights.
- The web interface and some generated text are currently Chinese; this language switch covers the README documentation.

## Further documentation

- [Implementation plan and acceptance criteria](IMPLEMENTATION_PLAN.md) — English.
- [System build guide](APPLYPILOT_SYSTEM_BUILD_GUIDE.md) — Chinese; design context and data contracts.
- [Browser application runbook](BROWSER_APPLICATION_RUNBOOK.md) — Chinese; execution and submission rules.
- [Usage and scheduling guide](HOWTO.md) — Chinese; local setup and scheduled searches.
- [Agent instructions](AGENTS.md) — repository operating rules.
