# Build challenge: plan ToolBox Poster — a queue-first Instagram operations studio

You are the founding engineer of a one-person startup. I am the product manager. Below is
the complete product brief for a system we are building **from scratch**. There is no
existing codebase, no legacy stack, and no engineering team. You own every technical
decision.

Your task right now is **not to write code**. Produce the complete engineering plan you
would actually execute, end to end, until the product is live. Engineers who operate a
production product with this behavior will review the plan adversarially. They will care
less about fashionable tools than whether your design remains correct when publishing,
payments, account access, workers, or outside services fail at the worst possible time.

Do not ask questions or offer menus of possible approaches. Where the brief is silent,
make a reasonable assumption, state it, and continue.

---

## 1. The product

**ToolBox Poster** helps creators and social-media operators keep one or more professional
Instagram accounts publishing consistently without living inside a calendar or sharing
their Instagram password with us.

The core idea is a queue, not a calendar. A user adds content, puts it in the order they
want, and defines when that account normally posts. The next eligible item takes the next
available slot. The product prepares the media, publishes it, records what happened, and
brings back performance results.

The product serves solo creators, theme-page operators, brands, local businesses, media
pages, and small agencies. The launch experience should feel simple for one person, while
still allowing a workspace to contain multiple people and multiple Instagram accounts.

The product's reputation rests on five promises, in this order:

1. **It publishes only what the customer intended.** A post must belong to the correct
   workspace and Instagram account, use the reviewed media and caption, and never publish
   twice because a button was double-clicked or work was retried.
2. **The queue tells the truth.** Order, scheduled time, progress, failures, pauses, and
   outside-platform limits must be visible. A post must not silently disappear, jump to a
   different account, or claim to be published when the outcome is uncertain.
3. **Uncertainty never becomes a second destructive action.** If the product loses contact
   at the moment an outside action may have succeeded, it must investigate or ask for help
   rather than blindly repeating the action.
4. **Holding or leaving never destroys the customer's work.** Pausing, disconnecting,
   losing access, changing plans, or encountering a temporary outage may hold work, but
   must not erase queued or published history. An explicit deletion request is the
   exception.
5. **Private access stays private.** Instagram passwords are never requested by the
   public product. Account permissions, customer media, private tokens, billing details,
   and restricted automation sessions must not leak across users, workspaces, logs, or
   support tools.

## 2. Users, operating shape, and constraints

- Launch is invite-only, with a public waitlist. Expect roughly 1,000 registered users,
  200 weekly active users, and several hundred connected Instagram accounts in one
  geographic region.
- Most customers have one account. Paying studios may have up to ten accounts and several
  collaborators.
- Normal launch traffic is hundreds to a few thousand prepared or published items per day,
  with bursts around common posting times.
- Images and short-form videos are the dominant storage and processing cost. Customers may
  build weeks of backlog and must be able to retrieve their original uploads.
- Instagram and its account limits are outside our control. Permissions expire, accounts
  disconnect, publishing can take time, limits change, and a timeout does not prove that a
  publish failed.
- One technical founder and one non-technical operator run the service. The initial
  infrastructure budget is a few hundred dollars per month. The operator must not need to
  edit production data or use a developer console for normal support.
- A responsive web product is sufficient. Desktop is the main planning surface; mobile is
  primarily for checking status, topping up a queue, pausing, and recovering an account.
- Accessibility, keyboard operation, and clear empty, loading, success, blocked, and error
  states are launch requirements.

## 3. Product requirements

### 3.1 Entry, identity, and workspaces

- The public site explains the product, its account requirements, pricing, safety posture,
  and content-rights rules. Interested users can join a waitlist.
- During the beta, access is granted by expiring, single-use invitations. Public account
  creation must not bypass the invitation gate.
- Accepting an invitation creates or joins a workspace. A first-time user completes a
  short onboarding flow about what they operate, their niche, their main goal, and desired
  posting cadence.
- A workspace owns its connected accounts, media, queue, history, subscription, and
  settings. A person may belong to more than one workspace.
- Workspace owners can invite collaborators. Roles should be simple enough for launch but
  must distinguish billing and destructive administration from day-to-day publishing.
- Suspended users or workspaces cannot continue product activity. Suspension holds work;
  it does not delete it.

### 3.2 Connecting Instagram accounts

- Users connect professional Instagram accounts through Instagram's own authorization
  experience. The public product never asks for an Instagram password.
- Before connection, the user may submit an account request for operator review. The
  operator can approve, decline, invite, or leave a clear reason without exposing internal
  notes to the requester.
- After authorization, the user chooses from the professional accounts available to them.
  The same Instagram account must not accidentally belong to two customer workspaces.
- The product shows account identity, profile image, connection health, publishing state,
  remaining daily capacity, cooldowns, and the next planned post.
- Permissions can expire or be revoked. When that happens, publishing pauses and queued
  work remains intact. The user receives a clear recovery path.
- Disconnecting an account is deliberate and visible. Reconnecting should preserve the
  relationship to the account's queue and history when ownership is verified.

### 3.3 Adding and preparing content

- Users can upload images and videos they own or have permission to publish, write or edit
  a caption, choose the destination account, and confirm their rights before queueing.
- Large uploads and media preparation take time. Users see progress and can leave the page
  without losing the operation.
- Unsupported, corrupt, oversized, or incompatible media is rejected with a useful reason.
  A preparation failure can be retried without creating a duplicate queue item.
- The product preserves the original media and creates a publish-ready version when
  needed. Account-level preparation preferences may include formatting media for Reels,
  fitting aspect ratios, removing unnecessary metadata, applying a customer-provided logo
  or banner, and choosing how captions are assembled.
- A queued item freezes the media, caption, attribution, destination, and preparation
  choices that will be used for that publish. Later account-setting changes must not
  silently alter already prepared work.
- Users may review a media preview and the final caption before publication.
- Every item retains its origin: direct upload or, for restricted workspaces, a tracked
  source. Source credit and a link to the original must remain available where relevant.

### 3.4 The queue and schedule

- Each Instagram account has its own ordered queue. Users can drag items into a new order,
  edit captions, hide and restore items, remove items, and perform sensible bulk actions.
- A user can publish the next item now or let the recurring schedule publish it later.
- Scheduling supports one or more rules per account: fixed times on selected days and
  repeating intervals inside a daily window. Rules use the timezone chosen for that
  account and show a preview of upcoming runs.
- Queue order and posting time are separate ideas. Reordering decides *what* is next;
  schedule rules decide *when* the next opportunity occurs.
- Changing a schedule must have a predictable effect on already queued items. Daylight
  saving changes, duplicated rules, skipped local times, and a timezone edit must not
  cause surprise double posts.
- Pausing an account stops new publications without deleting or failing queued items.
  Resuming continues from a clearly stated next slot.
- The product obeys both an account's configured daily allowance and Instagram's current
  restrictions. Reaching a limit or cooldown defers work instead of treating it as a
  permanent failure.
- The queue distinguishes preparing, ready, publishing, published, hidden, failed, and
  needs-review work. Users can safely retry known failures. An uncertain publish outcome
  goes to needs review and cannot be republished until reconciled.
- Status changes should feel live without requiring a page refresh.

### 3.5 Restricted content sourcing

This is an operator-enabled capability for selected tester workspaces, not a public launch
promise. The public product remains upload-first and must not suggest that users may take
arbitrary content or evade Instagram rules.

- An entitled workspace can add recurring sources by account, hashtag, or a general Reels
  feed. A source is verified before it becomes active.
- A source can limit media type and age, require minimum likes, comments, or plays, exclude
  captions containing configured words, control how many candidates are requested, and
  control how often it is checked.
- The system retains source identity, original link, author, caption, media type, observed
  engagement, and discovery time. It must avoid importing or queueing the same source post
  repeatedly.
- Eligible results enter a backlog rather than publishing immediately. Items held back by
  filters remain distinguishable from items ready to fill the queue.
- Users can refill the queue from the backlog manually. An account may also keep its queue
  near a chosen target automatically, stopping before it exceeds the intended depth.
- A user can request a one-time sample of recent Reels without creating a recurring source.
- Source verification, collection, and media retrieval can fail independently. Users see
  useful states such as pending verification, active, paused, retrying, or blocked.
- Access to this capability can be removed at any time. Removal stops new acquisition but
  does not erase already accepted customer work or its provenance.
- The engineering plan must explicitly address the legal, trust, platform-policy, abuse,
  isolation, and operational risks of this capability. Treating it as merely another
  import method is not acceptable.

### 3.6 Publishing and receipts

- Publishing happens outside the page request and can continue after the user closes the
  browser. An item displays meaningful progress from queued through final outcome.
- A successful publish creates a durable receipt containing the destination account,
  frozen content details, Instagram's identifier and permalink, timestamps, and enough
  evidence to support a customer dispute.
- The same intended item must never publish twice because of repeated clicks, concurrent
  workers, restarts, deploys, slow responses, or repeated outside callbacks.
- A timeout before sending is retryable. A timeout after the publish may have been accepted
  is not. The latter must enter reconciliation and preserve evidence about the attempt.
- Cancel works while publication is still safely cancelable. Once the outside action may
  have crossed the point of no return, the interface must say so rather than pretend it
  can undo the post.
- Publishing failures must distinguish customer-fixable problems, temporary outside
  problems, exhausted permissions, account limits, invalid media, and uncertain outcomes.
- A failed or delayed item must not block unrelated accounts forever. Heavy media work and
  restricted browser work must not starve normal publishing or the customer-facing app.

### 3.7 Library and analytics

- Each account has a library of posts published through the product. Users can view the
  final caption, media, publish time, Instagram permalink, original source when relevant,
  and lifecycle state.
- The product periodically brings back the performance measures Instagram makes available,
  including reach, views or plays, likes, comments, saves, shares, and totals derived from
  them where meaningful.
- Users can see current totals, change over time, engagement breakdowns, best and worst
  posts, and source-level performance. Comparisons must be honest about missing, stale, or
  incomparable measures.
- Analytics refreshes may lag without blocking publishing. Old snapshots remain useful for
  trends and investigation rather than being overwritten by the newest values.
- A manual refresh is available but cannot be used to exhaust outside limits or overload
  the service.

### 3.8 Managed cleanup of published content

This is an entitled, high-risk workflow. It applies only to posts originally published by
ToolBox Poster; it must never become a general-purpose tool for mutating arbitrary posts.

- Users can protect individual library posts so automated or manual cleanup will never
  select them.
- A cleanup rule identifies underperforming content using media kind, age, and one or more
  minimum performance measures. Its preview explains why each item qualifies and uses
  recent-enough analytics.
- The user reviews the exact selection and confirms it before a manual run begins. If the
  selection changes before execution, the old confirmation is no longer valid.
- Feed photos are archived. Reels are moved to Instagram's Recently Deleted area and may
  become permanently unavailable after Instagram's recovery window. The product explains
  this difference before confirmation and does not claim it can automatically restore a
  Reel.
- Cleanup runs one item at a time for an account, shows item-by-item results, and can be
  stopped before the next item begins. Stopping cannot reverse an action already accepted
  by Instagram.
- A cleanup may also run daily or weekly at an account-local time using saved rules. A
  scheduled occurrence rechecks permissions, connection health, fresh analytics, current
  protection, and whether another cleanup is active before doing anything.
- If the product crashes near an archive or delete action, it must not repeat an uncertain
  action automatically. The run pauses for reconciliation and later cleanup runs for that
  account remain ordered behind it.
- Cleanup history records the frozen rule, selected items, metrics used, who requested it,
  each result, and redacted evidence. Sensitive session material must never appear in that
  history.

### 3.9 Plans, billing, and entitlements

- There is a free entry plan and paid monthly or annual plans. Plans differ by connected
  account allowance, included collaborators, optional extra seats, storage or processing
  allowances, and access to advanced features such as managed cleanup.
- Billing belongs to the workspace. Only authorized workspace members can start checkout,
  change the subscription, or open the billing portal. Card details never pass through our
  product.
- Purchases, renewals, cancellations, payment failures, upgrades, downgrades, and repeated
  billing notifications must result in one understandable subscription state.
- Upgrades can take effect promptly. A downgrade or payment problem holds activity above
  the new limits but does not delete accounts, media, queue items, or history.
- Product access is based on the workspace's current effective entitlements, including
  temporary operator-granted beta access. Hiding a button is not sufficient enforcement.
- Billing pages show the current plan, interval, status, renewal or cancellation state,
  account and seat use, invoices where available, and the consequences of changing plan.

### 3.10 Notifications, feedback, account control, and deletion

- In-app notifications cover connection problems, publishing failures, uncertain outcomes,
  cleanup events, restricted-source problems, account limits, billing state, and items that
  need user attention. High-priority items are visually distinct.
- Notifications update without a manual refresh, have read and dismissed states, and deep
  link to the relevant account or work item.
- Users can submit feedback as a bug, confusing experience, idea, praise, or other note,
  with enough page context for the operator to investigate.
- Users can update personal and workspace settings, leave a workspace when allowed, export
  their data, disconnect Instagram, and request account deletion.
- Deletion revokes external access, cancels billing where appropriate, removes personal
  data and stored media, and provides a status reference without retaining the deleted
  data in disguise. Keep only the minimum non-identifying evidence needed to prove the
  request was completed.
- Privacy, terms, copyright, security-contact, and data-deletion pages are public before
  real customers are onboarded.

### 3.11 Operator and support needs

- A narrow platform-administrator area is separate from customer workspaces and requires
  stronger protection than an ordinary customer session.
- The operator can manage waitlist entries, invitations, connection requests, users,
  workspaces, suspensions, temporary feature access, feedback, and the restricted sourcing
  account pool.
- The operator sees whether the customer-facing app, publishing capacity, media processing,
  restricted automation, account connections, stored media, and backups are healthy.
- For a specific work item, the operator can determine where it is stuck, what has already
  happened, whether retry is safe, and what the customer has seen.
- Safe actions include retrying work known not to have crossed an outside side-effect,
  pausing a workspace or account, reconciling an uncertain outcome, revoking an invite,
  and adjusting a temporary entitlement. Privileged actions are attributable to an actor.
- Daily backups and a rehearsed restoration process are launch requirements. A green health
  screen is not a substitute for proving that customer data and media can be restored.

## 4. Hard product rules the plan must preserve

These are requirements, not aspirations:

- Tenant isolation applies to every user-facing and background action, including media
  links, live updates, notifications, analytics, and operator-assisted recovery.
- Every action that can publish, charge, archive, delete, invite, or change access is safe
  against repeated requests.
- Only one active publication of a queue item may cross the outside side-effect boundary.
- Only one cleanup item per Instagram account may cross its destructive boundary at a time.
- Known pre-action failures may retry; ambiguous post-action failures may not retry until
  reconciled.
- The media, caption, destination, settings, rule, and metrics a user approved are frozen
  for the action they approved.
- Queue work is held, not destroyed, when an account is paused, disconnected, suspended,
  over plan, over quota, or waiting for reauthorization.
- Daily publishing use cannot be forgotten by a restart or cache loss.
- Source content and uploaded content cannot be duplicated into the same account's queue
  through concurrent collection or refill activity.
- Restricted source and cleanup capabilities remain invisible and unreachable without an
  explicit workspace entitlement.
- Customer media is private by default. Any temporary outside access must be narrow and
  expire.
- Private account grants and restricted browser sessions are never returned to the browser,
  written to receipts, or included in ordinary logs.
- A customer's content-rights acceptance is attributable and versioned.
- External callbacks, billing events, and deletion requests can arrive repeatedly or out
  of order without corrupting state.
- Every final claim—published, archived, moved to Recently Deleted, charged, or deleted—has
  inspectable evidence.

## 5. Non-goals for v1

- No native mobile applications.
- No support for personal Instagram accounts.
- No publishing to social networks other than Instagram.
- No direct messages, comments, engagement automation, follower tools, or growth promises.
- No arbitrary Instagram password collection in the public product.
- No customer-facing marketplace of source content.
- No general social listening or competitor analytics product.
- No full creative editor; the product prepares and brands supplied media but is not a
  replacement for a video editor or design suite.
- No automatic generation of captions or media by artificial intelligence unless the
  engineering plan makes a compelling, budgeted, safety-conscious case. AI is not required
  to fulfill this brief.
- No requirement for global, multi-region operation at launch.

## 6. What your engineering plan must contain

Produce one self-contained plan using the following sections in this exact order:

1. **Technology decisions** — make one choice for each major part of the system. For every
   choice, name the strongest rejected alternative and explain why your choice better fits
   this product, scale, budget, and operator count.
2. **System architecture** — identify the independently running parts, their
   responsibilities, and how work moves between them. Show how customer pages, media work,
   ordinary publishing, restricted sourcing, and destructive cleanup avoid starving or
   endangering one another.
3. **Data model** — provide executable schema definitions with keys, relationships,
   constraints, and indexes. The model must make workspace ownership, frozen action inputs,
   deduplication, queue order, entitlements, leases, attempts, receipts, analytics history,
   billing, audit, and deletion behavior reviewable.
4. **Invariant enforcement map** — for every promise and hard rule above, name the concrete
   mechanism that enforces it and a specific test that proves it. This is the most heavily
   weighted section.
5. **Failure-mode walkthrough** — narrate numbered sequences for at least: a crash before
   publish, a crash after a publish may have been accepted, duplicate publish work, media
   preparation failure, account revocation, quota exhaustion, schedule edits near a slot,
   duplicate source collection, a browser hang during cleanup, a changed cleanup selection,
   repeated or out-of-order billing events, storage failure, and deletion interrupted
   halfway. End each walkthrough with the evidence an operator can inspect.
6. **AI strategy** — state whether AI belongs in v1. If it does, specify its bounded role,
   validation, privacy, quality measurement, fallbacks, and multiplied-out cost. If it does
   not, explain why deterministic product behavior is sufficient. Do not add AI merely to
   fill this section.
7. **Testing and release confidence** — specify what runs on every change, what requires
   real supporting services, what runs nightly, and what must be proven against safe test
   Instagram accounts. Include crash, retry, race, quota, timezone, restore, and ambiguous
   outcome drills.
8. **Delivery phases** — order the build as vertical slices, with objective exit evidence
   rather than dates. Identify the earliest phase that completes connect → upload → prepare
   → queue → publish → receipt → analytics against a safe test account, and justify every
   phase before it.
9. **Security and privacy** — cover identity, authorization, workspace isolation, secrets,
   private media, restricted sessions, billing callbacks, abuse controls, audit, retention,
   export, deletion, and support access.
10. **Risk register** — list the ten risks most likely to sink the product, their early
    warning signal, and a mitigation already present in the plan.
11. **Explicit tradeoffs** — disclose every place the plan intentionally provides weaker
    behavior than this brief and why the trade is acceptable for v1.
12. **Where this is stronger than required** — identify deliberate improvements beyond the
    brief and justify their product value and cost.
13. **Assumptions** — collect every decision made where the product brief was silent.

## 7. Ground rules for the plan

- Make every technical decision yourself. Do not ask me questions, leave a `TBD`, or
  present several interchangeable options.
- Show mechanisms, not assurances. If a claim depends on ordering, a transaction, a
  uniqueness rule, a lease, a state transition, or retained evidence, name it precisely.
- Use numbers and derivations for concurrency, timeouts, retries, rate limits, retention,
  queue alarms, storage growth, and monthly cost. Do not hide behind words such as
  “reasonable,” “small,” or “scalable.”
- Treat Instagram, the payment provider, email, object storage, and the network as
  unreliable outside systems. A successful request, a failed request, and an unknown
  outcome are three different states.
- Optimize for a one-person operation and the stated launch scale. Added complexity must
  earn its place by protecting a product rule or reducing a concrete operational burden.
- The public upload-only experience and restricted automation experience must be separable
  in product access, operations, testing, and failure containment.
- Do not assume that hiding a feature in the interface enforces access.
- Do not claim exactly-once delivery from an unreliable network. Explain how the product
  prevents duplicate customer-visible effects and resolves uncertainty.
- Do not weaken a requirement silently. Put every conscious compromise in the explicit
  tradeoffs section.
- The completed plan must be specific enough that a competent engineer could begin the
  first delivery phase tomorrow without asking what you meant.
