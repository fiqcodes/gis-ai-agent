# Orbiview on Vercel

The web app is a public portfolio demo: no login or access password is required.
Flask runs on Python 3.12 with a 300-second function limit. The original web GIS
pipeline is retained in `analysis.py`; `agent.py` remains the LangGraph CLI.
Both use Groq instead of Ollama. Gemma 3 4B is not listed in Groq's model catalog;
`openai/gpt-oss-120b` is the configurable default. Text reasoning, JSON parsing,
GIS interpretation and report writing are retained. The existing app did not
send images to the model; this migration does not add image understanding.
Model equivalence has not been established by benchmark.

## Configure after the first deployment

In Vercel -> gis-ai-agent -> Settings -> Environment Variables, select Production:

| Name | Value |
| --- | --- |
| `GROQ_API_KEY` | Your Groq API key |
| `GEE_PROJECT` | Earth Engine registered Google Cloud project ID; currently `gis-ai-agent` |
| `GEE_SERVICE_ACCOUNT_JSON` | Complete JSON contents of the service-account key used for this Earth Engine project |
| `APP_SECRET_KEY` | Random signing secret of at least 32 characters; provisioned by the deployment workflow |
| `UPSTASH_REDIS_REST_URL` | Upstash REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash read/write REST token |

The Vercel Upstash integration's `KV_REST_API_URL` and `KV_REST_API_TOKEN` aliases
are also accepted. A separate database is preferable; if sharing a database,
Orbiview uses its own prefix and never reads SmartLook keys. Preview has a separate
prefix and requires its own environment configuration. Never commit credentials.

The Earth Engine API must be enabled, and the Cloud project registered for Earth
Engine. The service account needs the relevant Earth Engine permissions and
permission to consume the project's services. Cloud account registration, billing
eligibility and quota decisions must be completed by the owner. Health checks only
verify configuration presence, not IAM access or provider availability.

After adding variables, redeploy. The first setup-only deployment opens the UI but
returns 503 for analyses until configuration is present. Redis failures fail closed.

## Request lifetime and storage

The browser consumes newline-delimited JSON progress on the original POST request.
A supervised Python subprocess runs each GIS/report job with a unique temporary
directory, isolating matplotlib and Earth Engine state. It is terminated on timeout
or when the response generator closes; there are no detached background jobs.
The application deadline is 260 seconds, leaving time before Vercel's 300-second
limit. Upstream proxy disconnect detection can be delayed; the hard deadline remains.

Completed results and PDFs are compressed and split into small Redis REST chunks,
bound to the visitor's signed cookie and retained for 24 hours. Report requests
accept a server-side result ID, never arbitrary client-supplied research data.
Local files are cleaned up when the worker exits. PDF downloads are streamed.

Limits: 3 requests/session/minute, 5/IP/minute, 50 analyses/day, 10 reports/day.
Daily counts reset at midnight UTC and include failed attempts. Each request accepts
1–3 indices and a region at most 3 degrees across; explicit multi-year comparisons
run sequentially for at most 5 years. Complex GEE work can still time out: reduce the
region or date range. The limits bound public-demo use, not a guaranteed money budget.

## Verification

Run `.venv/bin/python -m pytest -q` and `node --check public/static/js/app.js`.
Tests exercise Groq request translation, lazy GEE authentication, existing Q&A flow,
actual PDF assembly with stubbed prose, streaming, real worker processes, cancellation,
timeouts, Redis chunks, per-visitor ownership and request limits.

Local tests do not prove live Groq, Earth Engine or Upstash access. After credentials
are configured, verify: Q&A; small single-index analysis; a drawn polygon; a short
multi-year comparison; PDF generation/download; isolation in another browser.
The staged production build must be tested before promoting that same build.

Sources: [Groq models](https://console.groq.com/docs/models),
[Earth Engine service accounts](https://developers.google.com/earth-engine/guides/service_account),
[Vercel function limits](https://vercel.com/docs/functions/limitations).
