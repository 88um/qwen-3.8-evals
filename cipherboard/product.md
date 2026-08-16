# Build challenge: plan "Cipherboard" — offline-first end-to-end-encrypted collaboration

You are the founding engineer; I am the product manager. Greenfield build. Your
task is not code but the complete engineering plan, following
`engineering-plan-standard.md` exactly (13 sections, all rules). The reviewers
include a cryptography engineer whose entire job is finding the place where a
plan quietly claims an impossible property. This brief contains several
requirements that are in *tension* with each other; the product's credibility —
and your grade — rests on resolving each tension with a mechanism or naming it
honestly as a limit. A plan that claims all of them simultaneously without
noticing the conflicts fails regardless of its other qualities.

**Output cap: 12,000 tokens.** Over-cap plans are truncated where they stand.

---

## 1. The product

**Cipherboard** is a collaborative notes and workspace product — documents,
boards, attachments — where **the server can never read customer content**.
Devices work fully offline and sync when connected. Teams share workspaces,
invite and remove members, lose phones, forget passwords, and expect all of it
to just work. We sell to security-conscious teams precisely on the claim that a
subpoena served on us yields ciphertext.

The promises, in order:

1. **The server never possesses plaintext content or content keys.** Not in
   memory during normal operation, not in logs, not in backups, not during
   search, not during recovery. Every feature below must be delivered *under*
   this constraint or explicitly scoped down in §11.
2. **Offline edits are never lost.** Any device offline up to 30 days merges its
   edits without data loss when it returns; concurrent edits to the same
   document converge to the same result on every device without user-visible
   corruption.
3. **Membership changes mean something.** A removed member or revoked device
   must not be able to read **future** changes. Whatever a revoked party could
   already read is a separate, honest conversation (§11).
4. **Lockout is recoverable without becoming escrow.** A user who forgets their
   password can regain access to their content through a mechanism the plan
   defines — and that mechanism must not quietly hand us (or anyone who
   compromises us) the ability to decrypt customer content. If your recovery
   design gives the provider a decryption path, promise 1 is broken and §11
   must say so in those words.

## 2. Users and scale

- Launch: 2,000 teams, median 8 members; largest shared workspace: **100
  collaborators**. A user may have up to **10 devices**; assume phones, laptops,
  and a browser client.
- Documents: median 40 per workspace, p99 2,000. Attachments to 100 MB.
  Edit rate in an active document: bursts of 10 ops/sec from 5 concurrent
  editors.
- Devices may be offline up to **30 days** and must sync forward from wherever
  they left off. History matters: users see per-document version history and can
  restore prior versions.
- **Search is a launch requirement**: a user searches across every document
  they can access, from any of their devices, including content authored by
  teammates while that device was offline. Design it under promise 1 — and if
  your search design weakens promise 1 (e.g., anything the server can use to
  infer content), quantify the leak in §9 rather than hoping nobody asks.
- One small team operates this; infrastructure is boring by intent (managed
  Postgres + object storage + stateless API nodes). §6 (AI strategy): no AI
  features at v1; one line per the standard.

## 3. What the plan must cover

- **Key hierarchy** (§3/§4): every key class (account, device, workspace,
  document, attachment, search — as applicable to your design), who generates
  it, where it lives, what wraps what, expressed concretely enough that a
  reviewer can trace "device D reads document X" and "member M is removed" as
  chains of named keys and named messages. DDL for the server's tables (R2
  applies: the server stores ciphertext and metadata — show exactly which
  columns are which).
- **Sync and convergence model** (§3/§5): the CRDT or OT choice (R1: strongest
  rejected alternative), what an "op" is, how 30-day-offline devices catch up,
  how history and version restore work, and — named mechanism — what bounds
  metadata growth: tombstones, op-log compaction, and how compaction coexists
  with devices that were offline across the compaction horizon.
- **Membership and revocation protocol** (§4/§5): the exact sequence for
  add-member, remove-member, revoke-device, and key rotation, including what
  happens when **rotation is initiated while half the collaborators are
  offline** and when **two admins change membership concurrently**. State
  explicitly what a *new* member can read: if your design grants new members
  full history, say so; if it grants forward-only access, show the key
  boundary that enforces it.
- **Recovery design** (§4/§9): the concrete mechanism (recovery codes, social
  recovery, hardware keys — commit to one per R6), what exactly it can and
  cannot decrypt, and the precise statement of what an attacker who fully
  compromises the server gains against a recovering user.
- **Attachment pipeline** (§3): encryption, chunking, dedup posture (state
  whether dedup exists and what it leaks if it does), and resumable upload
  from flaky mobile connections.
- **Metadata honesty** (§9): a table of what the server *does* learn under your
  design — workspace membership graphs, document sizes, edit timing, access
  patterns, search behavior — because "E2EE" marketing dies on undisclosed
  metadata. This table is graded as a mechanism: complete and specific beats
  short and flattering.
- **Ten numbered worst-window walkthroughs** (§5, R7), which must include at
  least: revoked device with cached keys attempting reads and writes; rotation
  racing an offline collaborator's return; recovery executed while the
  account's devices are all lost; two admins removing each other concurrently;
  a device restored from an old backup replaying stale ops; and the server
  itself acting maliciously (what breaks, what provably cannot).

## 4. Required honesty: the admissions table

§11 must contain a table answering **yes/no, with one sentence each**, at
minimum for these claims — these are graded mechanically as admissions, and a
missing row is scored as a hidden weakening (R11):

| # | Claim your plan must answer |
|---|---|
| A1 | A revoked device can still read content it synced before revocation |
| A2 | Key rotation completes only when all members have been online since rotation began |
| A3 | A new member can read history from before they joined |
| A4 | The recovery mechanism gives the provider (or a server compromiser) any decryption capability |
| A5 | Server-side components can rank/filter/index plaintext content in any form |
| A6 | Deleted content remains recoverable from op history or backups after the user deletes it |
| A7 | The server can withhold or reorder updates without clients detecting it |
| A8 | Metadata (titles, sizes, timing, membership) is visible to the server |

There are correct designs on both sides of several of these rows. The failure
mode is not the honest "yes" — it is the "no" that your own §3 contradicts.

## 5. Grading

| What | Weight |
|---|---:|
| §4 Invariants: key boundaries, revocation, convergence (with the admissions table cross-checked against §3) | 30 |
| §3 Data model: key hierarchy, DDL, op/history model | 20 |
| §5 Worst-window walkthroughs | 15 |
| §9 Security, threat model, metadata honesty | 10 |
| §8 Delivery phases (first end-to-end slice justified) | 10 |
| §7 Testing: named tests incl. convergence property tests and revocation drills | 10 |
| Capacity arithmetic: op-log growth, compaction, attachment storage, sync bandwidth for a 30-day-offline device | 5 |

Mechanical checks reviewers will run: every admissions-table answer is diffed
against the §3/§4 design (a contradiction voids the row and costs double); the
key-trace for "revoked member reads future doc" must dead-end at a named key
the member lacks; op-log growth arithmetic is recomputed; every library
capability claim (R10) is checked against that library's documentation.
