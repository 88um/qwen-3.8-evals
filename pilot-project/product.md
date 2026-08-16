# Build challenge: plan "Pilot" — a resume-first job application engine

You are the founding engineer of a one-person startup. I am the product manager. Below is
the complete product brief for a system we are building **from scratch**. There is no
existing codebase, no legacy constraints, and no team — you own every technical decision.

Your task right now is **not to write code**. It is to produce a complete engineering plan
for the product described below: the plan you would actually execute, end to end, until
the product is live. Your plan will be adversarially reviewed by engineers who operate a
production system with this exact behavior. They will probe every mechanism. Vague
answers, unresolved "TBD"s, and option menus ("we could use X or Y") score zero — commit
to decisions and defend them.

---

## 1. The product

**Pilot** applies to jobs for you, without lying and without acting behind your back.

A job seeker uploads their resume once. Pilot turns it into a structured, editable
profile, continuously discovers real job postings, ranks them against the profile with an
explanation of fit, generates tailored application documents that are provably truthful,
and — with the user's explicit authorization — fills out and submits the actual
application forms on employers' hiring sites using browser automation. Users pay per
successful submission through a credit system.

The product's reputation rests on three promises, in this order:

1. **It never lies on your behalf.** Every claim in a generated resume or cover letter
   must be traceable to something the user actually said about themselves. If a job
   requires a credential the user lacks, the documents must not paper over it.
2. **It never acts without you.** No application is ever submitted without an explicit,
   per-application user authorization. "Set and forget" exists, but the user grants it
   per job at queue time, knowingly, and it still refuses to proceed when required
   questions have no confident answer.
3. **It never double-submits.** An employer receiving the same application twice, or a
   half-filled ghost submission, is a catastrophic user-trust failure. This must hold
   even when infrastructure crashes at the worst possible moment.

## 2. Users and scale

- Launch target: ~1,000 registered users, ~200 weekly active, single region.
- Job discovery: ~500 company career boards monitored (initial market: companies hosted
  on Greenhouse-style ATS platforms with public board pages and per-job apply forms),
  ~5,000–20,000 postings ingested/refreshed per day.
- Application volume: tens to low hundreds of prepared applications per day; a handful of
  browser automation sessions running concurrently is acceptable at launch.
- One operator (me, non-engineer on ops matters) runs this. It is self-hosted on one or
  two rented machines. Cost matters: infrastructure budget is a few hundred dollars a
  month, and LLM spend should stay roughly under $0.10 per prepared application at
  current commodity-model pricing. Cheap models with quality fallbacks are fine; the plan
  should say how model quality is measured, not just asserted.

## 3. Feature requirements

### 3.1 Onboarding and profile
- Sign up with email/password. Upload a resume (PDF or DOCX). The system extracts it
  into structured profile data: contact info, work history, education, skills, licenses,
  certifications, projects.
- Extraction will be imperfect, so the profile is **user-correctable**: the user reviews
  and edits everything in a draft before it takes effect. Nothing downstream (matching,
  documents, applications) sees profile data until the user explicitly publishes it.
- Users re-upload resumes and edit their profile over time. The system must always be
  able to answer, for any generated document or submitted application: *which version of
  the profile was this based on, and what did it say?* An edit today must not silently
  change the basis of an application prepared yesterday.
- The profile includes reusable application preferences: work authorization, relocation,
  notice period, salary expectations, and standard consent answers.
- Voluntary demographic self-identification (EEO-type questions: gender, ethnicity,
  veteran status, disability, etc.) is collected separately, is always optional, and is
  **quarantined**: it must never be inferred by AI, never sent to any AI model, never
  used in matching or ranking. It may be used to fill an application's demographic
  section only by exact, unambiguous option matching, and anything ambiguous is left for
  the user.

### 3.2 Discovery, matching, and the feed
- The system finds company boards (seeded + automatically discovered), monitors them on a
  schedule, ingests new postings, updates changed ones, and closes ones that disappear.
- Each posting is enriched into structured data: location(s), remote/hybrid/onsite,
  employment type, salary if stated, and concrete requirements.
- Every published profile gets a ranked feed of matches with a **human-readable
  explanation** of why this job fits and where it falls short. Users can filter the feed
  (remote, location, salary…), dismiss jobs (and restore them), and share/bookmark a
  filtered view via URL. Ranking quality is a product feature: the plan must include how
  ranking quality is measured and regression-tested, not just which algorithm is used.
- Location logic must be respectful of reality: don't rank an onsite job on another
  continent above a local one, and don't hide jobs on weak inference. When unsure, keep
  it visible and let the user decide.

### 3.3 Tailored documents
- Per job, the user can generate a tailored resume and cover letter. These are built from
  the published profile — never from the raw resume file — and every line must survive
  the truthfulness audit in §1. Output is downloadable PDF and DOCX, professional
  quality, and must handle real names and content in any language (accents, non-Latin
  scripts).
- Generation is asynchronous with visible progress; failures are explained and
  retryable. Users can regenerate, review, and approve documents before use.

### 3.4 Applying
- From a match, the user queues an application. The system then, asynchronously:
  ensures a tailored resume exists, opens the employer's real application form, reads its
  actual fields, and drafts an answer for every field it can, using the profile, the
  reusable preferences, and the tailored documents. Uploads, dropdowns, multi-selects,
  free-text questions, and consent checkboxes all occur in the wild.
- The result is a **review screen**: the user sees the exact form the employer will
  receive, edits any answer, must fill required fields the system declined to guess, and
  then explicitly submits. Only that per-application human authorization lets automation
  cross the line and file it.
- "Full auto" mode: a user can grant the submit authorization up front, at queue time,
  per job, with unmistakable labeling. Even then the system self-blocks — surfacing a
  review to the user instead of proceeding — whenever any required field lacks a
  confident answer, or any consent/sensitive field would require guessing.
- The reviewed form must be what gets submitted. If the employer's form changes between
  review and submission, the system must detect it and block rather than submit stale or
  mismatched answers.
- Some employers challenge submission with an emailed one-time code. The product
  experience: the user is alerted, enters the code in Pilot within a few minutes, and
  submission continues. Your plan must handle this without turning it into a resource or
  reliability problem.
- Every application has a visible lifecycle (queued → preparing → ready for review →
  submitting → submitted / failed / canceled, or your equivalent) with live-feeling
  status updates, a full audit trail of what the automation did (including proof of what
  was submitted), and sane behavior for cancel and retry at every stage.
- The three promises of §1 are hard requirements here. Crashes, restarts, deploys, or
  duplicate background jobs must never produce a duplicate or unauthorized submission.
  Interrupted work must be recovered automatically — an application stuck forever in
  "submitting" is almost as bad as a double-submit.

### 3.5 Credits and billing
- Users receive a small welcome balance, then buy credit packs or a subscription via a
  standard payment provider (e.g. Stripe). Card data never touches our servers.
- A credit is *held* when an application is queued, *charged* only when submission
  succeeds, and *released* on failure or cancellation. Balances can never go negative and
  double-charging must be impossible, including under webhook retries and concurrent
  activity. Purchases, charges, and releases are visible to the user as a ledger.

### 3.6 Notifications, account, and operations
- In-app notifications (application ready for review, submitted, failed, needs code,
  credits low) with an unread indicator, updating without a manual page refresh.
- Account settings; full data export; account deletion that actually deletes (external
  subscriptions canceled, stored files removed, personal data gone) while retaining a
  minimal anonymized audit record of the deletion itself.
- Uploaded resumes and extracted personal text are sensitive; protect them at rest.
- Operator needs: I must be able to see system health (queue depths, worker liveness,
  automation capacity, error rates, backup freshness), diagnose why any specific
  application is stuck, and requeue or fail things safely. Daily backups with a tested,
  documented restore path are launch requirements, not afterthoughts.
- An admin role exists but is narrow; regular staff tooling is not needed at launch.

## 4. Non-goals for v1
- No mobile apps; responsive web is enough. No teams/recruiter side. No job-board
  ATS coverage beyond the initial platform target (but don't paint yourself into a
  corner). No social features. No resume-from-scratch authoring.

## 5. What your plan must contain

Produce one self-contained plan document with, at minimum:

1. **Technology decisions** — language(s), frameworks, datastores, queue/async approach,
   browser automation tooling, AI model strategy, hosting/deployment shape. For each
   major decision: the choice, the strongest rejected alternative, and why. Decisions
   must fit the one-operator, few-hundred-dollars constraint.
2. **System architecture** — processes, their responsibilities, and how they communicate.
   Explain how browser-heavy work is kept from starving everything else.
3. **Complete data model** — every entity, key relationships, and where each hard
   invariant (versioning, holds, snapshots, audit) lives in the schema.
4. **Invariant enforcement map** — a table taking each product promise and hard rule in
   §1 and §3 (truthfulness, review gate, no-double-submit, form-drift block, credit
   safety, EEO quarantine, profile version pinning, crash recovery, OTP flow) and stating
   the *specific mechanism* that enforces it and *what evidence proves it works*. This
   section is weighted most heavily in review.
5. **Failure-mode walkthrough** — narrate concretely: what happens when the machine dies
   mid-submission? When the same job runs twice? When the LLM returns garbage or times
   out? When the employer's form changes after review? When a webhook is replayed? When
   the browser hangs? Each answer must name the mechanism, not a hope.
6. **AI quality strategy** — how prompts/models are chosen and validated, how output is
   constrained and checked (especially the truthfulness audit), how quality is measured
   over time, and how cost is kept within budget.
7. **Testing and release confidence** — the test pyramid you'll actually build, what runs
   on every change vs. nightly, and how you prove the no-double-submit and
   crash-recovery guarantees before real users are exposed. "We'll write unit tests" is
   not an answer.
8. **Delivery phases** — the order you build in, with each phase closed by demonstrable
   evidence rather than a date. Identify the earliest point at which the full end-to-end
   loop (signup → upload → match → prepare → review → submit) runs against a safe fake
   employer, and justify everything you place before it.
9. **Security and privacy model** — authn/authz, tenant isolation, secrets, encryption
   at rest choices, webhook verification, abuse/rate limiting.
10. **Risk register** — the ten things most likely to sink this product technically, each
    with a mitigation you've actually planned (not "monitor closely").

## 6. Ground rules

- Make every technical decision yourself; do not ask me questions or present options.
  Where the brief is silent, state your assumption inline and proceed.
- Be specific enough that a competent engineer could start building from your plan
  tomorrow without asking what you meant.
- Do not gold-plate: anything beyond the brief must earn its place with a product
  rationale. Simplicity that survives an adversarial review beats architecture astronomy.
- The plan will be judged against a production system whose behavior matches this brief.
  Where your design is *better* than the brief implies, say so and say why; where you
  are consciously accepting a weaker behavior for v1, flag it as an explicit tradeoff
  rather than hoping nobody notices.
