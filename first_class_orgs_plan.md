# First-Class Organizations + Workspaces: Final Consolidated Implementation Plan

## Status & Resumption

- **Baseline repo**: origin/dev at commit `ca74f980c`, dated March 28, 2026 08:12:33 UTC.
- **Current step**: PR1 — Schema Foundation + Tenant Context Plumbing.
- **No implementation has started.** This document is the single source of truth.
- **How to resume**: Confirm branch contains this plan, pull latest origin/dev with `git pull --no-rebase origin dev`, then begin the current step. If a product decision is not stated here, use the Explicit Defaults section at the end.
- **After each PR merges**, update only: Current step, merged PR number, merge SHA.

---

## Part 1: Locked Product Rules

These rules are final. No implementer should override them.

### Organization Rules
1. Every authenticated platform action runs inside exactly one active organization.
2. Every existing user is migrated into a personal organization whose slug matches the user's current username/profile slug.
3. If a personal-org slug collision occurs, use a deterministic numeric suffix and record the legacy slug as a redirect alias via `OrganizationAlias`.
4. Personal orgs are convertible to team orgs (one-way upgrade, `isPersonal` flips to `false`).
5. Private by default is the universal default for agents, chats, runs, credentials, and all tenant-owned content.
6. No resource may be shared across organizations.
7. Cross-org movement is allowed only through transfer flows (direct if actor is admin on both orgs; otherwise request/approval).

### Workspace Rules
8. Organizations can contain many workspaces. Workspaces are first-class primitives, not tags or folders.
9. Workspaces have `OPEN` or `PRIVATE` join policy (Linear-inspired).
10. Open workspaces are discoverable and self-joinable by org members.
11. Private workspaces require invite or admin action.
12. Every org has exactly one default workspace (`isDefault=true`, `OPEN`). All new org members auto-join the default workspace.
13. The default workspace cannot be deleted.
14. Workspace creation is allowed for org admins and org billing managers.
15. A workspace creator becomes a workspace admin automatically.

### Context & Navigation Rules
16. An active workspace is optional and always scoped to the active organization.
17. Org home (no workspace selected) shows the user's private org-root resources plus org-shared resources. It does NOT aggregate all workspace resources.
18. On org switch, restore the last-used accessible workspace for that org; otherwise land in org home.
19. Org switcher lives in the avatar/account menu. Workspace picker is nearby in the same UI surface.
20. Request headers: `X-Org-Id` and `X-Workspace-Id`. Cookies: `active-org-id` and `active-workspace-id`. Keep `X-Act-As-User-Id` for admin impersonation.

### Sharing & Visibility Rules
21. Visibility values: `PRIVATE`, `WORKSPACE`, `ORG`.
22. Valid container/visibility combinations:
    - `orgId + workspaceId=null + PRIVATE` → private org-root resource
    - `orgId + workspaceId=null + ORG` → org-shared resource
    - `orgId + workspaceId=set + PRIVATE` → private workspace-contained resource
    - `orgId + workspaceId=set + WORKSPACE` → workspace-shared resource
23. Invalid: `workspaceId=set + ORG` or `workspaceId=null + WORKSPACE`.
24. Sharing changes only visibility, not ownership. Cross-org sharing is forbidden at every layer.
25. Resource creation never defaults to shared.

### Marketplace & Publishing Rules
26. Published agents must be owned by organizations, not users.
27. Existing published agents re-slug from `user_slug/agent_slug` to `org_slug/agent_slug`.
28. Old published URLs keep working through permanent aliases/redirects via `OrganizationAlias`.
29. Workspace-origin agents must be promoted to org-shared before publishing. Direct publish from workspace is forbidden in v1.
30. Alias removal is platform-admin-only and audited.

### Credential Rules
31. Existing credentials migrate as user-scoped credentials inside the user's personal org (not org-shared).
32. Credential scopes: `USER`, `WORKSPACE`, `ORG`.
33. Resolution order (fixed): `USER > WORKSPACE > ORG`.
34. Cross-org transfer does not move credential payloads. Transferred resources require manual reattachment.

### Billing & Seat Rules
35. Billing is org-owned. Stripe customer/subscription, balances, credits, top-up → all org scope.
36. Seats are org-level assignments to users. Seats gate active product use, not membership.
37. Free seats are valid seats with low entitlements. Seatless users may remain org members.
38. Billing-only users may remain seatless.

### Role Model Rules
39. Roles use a single enum, not boolean flags.
40. Org roles: `OWNER`, `ADMIN`, `BILLING_MANAGER`, `MEMBER`.
41. Admin inherits all billing manager capabilities. No need for dual assignment.
42. Role hierarchy: `OWNER > ADMIN > BILLING_MANAGER > MEMBER`.
43. Workspace roles: `ADMIN`, `BILLING_MANAGER`, `MEMBER`.
44. Workspace admin inherits all workspace billing manager capabilities.

### API Key & OAuth Rules
45. API keys and OAuth apps: `ownerType = USER | ORG`.
46. Org-owned keys/apps are org-wide by default, may optionally restrict to one workspace.
47. Existing keys/apps migrate as user-owned inside the personal org.

### Portability Rules
48. Copy within org (same or different workspace).
49. Fork across orgs (creates independent copy).
50. Admin transfer between orgs (ownership change with audit trail via `TransferRequest`).
51. Offboarding: resources stay in workspace when user leaves.

### Naming Rules
52. The new collaborative workspace model is named `Workspace` (the clean name).
53. The existing file-storage `UserWorkspace` model is renamed to `FileSpace`.
54. The `FileSpace` rename happens in PR1 (schema). `workspace://` URI compatibility is preserved at the API level.

---

## Part 2: Canonical Data Model & Prisma Schema

### New Enums

```prisma
enum OrgMemberRole {
  OWNER
  ADMIN
  BILLING_MANAGER
  MEMBER
}

enum WorkspaceMemberRole {
  ADMIN
  BILLING_MANAGER
  MEMBER
}

enum WorkspaceJoinPolicy {
  OPEN
  PRIVATE
}

enum ResourceVisibility {
  PRIVATE
  WORKSPACE
  ORG
}

enum CredentialScope {
  USER
  WORKSPACE
  ORG
}

enum CredentialOwnerType {
  USER
  WORKSPACE
  ORG
}

enum OrgAliasType {
  MIGRATION
  RENAME
  MANUAL
}

enum OrgMemberStatus {
  INVITED
  ACTIVE
  SUSPENDED
  REMOVED
}

enum SeatType {
  FREE
  PAID
}

enum SeatStatus {
  ACTIVE
  INACTIVE
  PENDING
}

enum TransferStatus {
  PENDING
  SOURCE_APPROVED
  TARGET_APPROVED
  COMPLETED
  REJECTED
  CANCELLED
}
```

### New Models

```prisma
model Organization {
  id                   String   @id @default(uuid())
  createdAt            DateTime @default(now())
  updatedAt            DateTime @updatedAt
  name                 String
  slug                 String   @unique
  avatarUrl            String?
  description          String?
  isPersonal           Boolean  @default(false)
  settings             Json     @default("{}")
  stripeCustomerId     String?
  stripeSubscriptionId String?
  topUpConfig          Json?
  archivedAt           DateTime?
  deletedAt            DateTime?
  bootstrapUserId      String?

  Members              OrgMember[]
  Workspaces           Workspace[]
  Aliases              OrganizationAlias[]
  Balance              OrgBalance?
  CreditTransactions   OrgCreditTransaction[]
  Invitations          OrgInvitation[]
  StoreListings        StoreListing[]         @relation("OrgStoreListings")
  Credentials          IntegrationCredential[]
  Profile              OrganizationProfile?
  Subscription         OrganizationSubscription?
  SeatAssignments      OrganizationSeatAssignment[]
  TransfersFrom        TransferRequest[]      @relation("TransferSource")
  TransfersTo          TransferRequest[]      @relation("TransferTarget")
  AuditLogs            AuditLog[]

  @@index([slug])
}

model OrganizationAlias {
  id               String       @id @default(uuid())
  organizationId   String
  aliasSlug        String       @unique
  aliasType        OrgAliasType
  createdAt        DateTime     @default(now())
  createdByUserId  String?
  removedAt        DateTime?
  removedByUserId  String?
  isRemovable      Boolean      @default(true)

  Organization     Organization @relation(fields: [organizationId], references: [id], onDelete: Cascade)

  @@index([aliasSlug])
  @@index([organizationId])
}

model OrganizationProfile {
  organizationId   String       @id
  username         String       @unique
  displayName      String?
  avatarUrl        String?
  bio              String?
  socialLinks      Json?
  createdAt        DateTime     @default(now())
  updatedAt        DateTime     @updatedAt

  Organization     Organization @relation(fields: [organizationId], references: [id], onDelete: Cascade)
}

model OrgMember {
  id               String          @id @default(uuid())
  createdAt        DateTime        @default(now())
  updatedAt        DateTime        @updatedAt
  orgId            String
  userId           String
  role             OrgMemberRole   @default(MEMBER)
  status           OrgMemberStatus @default(ACTIVE)
  joinedAt         DateTime        @default(now())
  invitedByUserId  String?

  Org              Organization    @relation(fields: [orgId], references: [id], onDelete: Cascade)
  User             User            @relation(fields: [userId], references: [id], onDelete: Cascade)
  SeatAssignment   OrganizationSeatAssignment?

  @@unique([orgId, userId])
  @@index([userId])
  @@index([orgId, role])
  @@index([orgId, status])
}

model OrgInvitation {
  id              String   @id @default(uuid())
  createdAt       DateTime @default(now())
  orgId           String
  email           String
  targetUserId    String?
  role            OrgMemberRole @default(MEMBER)
  token           String   @unique @default(uuid())
  tokenHash       String?
  expiresAt       DateTime
  acceptedAt      DateTime?
  revokedAt       DateTime?
  invitedByUserId String
  workspaceIds    String[]

  Org             Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)

  @@index([email])
  @@index([token])
  @@index([orgId])
}

model Workspace {
  id              String              @id @default(uuid())
  createdAt       DateTime            @default(now())
  updatedAt       DateTime            @updatedAt
  name            String
  slug            String?
  description     String?
  isDefault       Boolean             @default(false)
  joinPolicy      WorkspaceJoinPolicy @default(OPEN)
  orgId           String
  archivedAt      DateTime?
  createdByUserId String?

  Org             Organization        @relation(fields: [orgId], references: [id], onDelete: Cascade)
  Members         WorkspaceMember[]
  Invites         WorkspaceInvite[]

  // Resource relations
  AgentGraphs     AgentGraph[]
  Executions      AgentGraphExecution[]
  ChatSessions    ChatSession[]
  Presets         AgentPreset[]
  LibraryAgents   LibraryAgent[]
  LibraryFolders  LibraryFolder[]
  Webhooks        IntegrationWebhook[]
  APIKeys         APIKey[]
  Credentials     IntegrationCredential[] @relation("WorkspaceCredentials")

  @@unique([orgId, name])
  @@index([orgId, isDefault])
  @@index([orgId, joinPolicy])
}

model WorkspaceMember {
  id               String              @id @default(uuid())
  createdAt        DateTime            @default(now())
  updatedAt        DateTime            @updatedAt
  workspaceId      String
  userId           String
  role             WorkspaceMemberRole  @default(MEMBER)
  status           OrgMemberStatus     @default(ACTIVE)
  joinedAt         DateTime            @default(now())
  invitedByUserId  String?

  Workspace        Workspace           @relation(fields: [workspaceId], references: [id], onDelete: Cascade)
  User             User                @relation(fields: [userId], references: [id], onDelete: Cascade)

  @@unique([workspaceId, userId])
  @@index([userId])
  @@index([workspaceId, role])
}

model WorkspaceInvite {
  id               String              @id @default(uuid())
  createdAt        DateTime            @default(now())
  workspaceId      String
  email            String
  targetUserId     String?
  role             WorkspaceMemberRole  @default(MEMBER)
  token            String              @unique @default(uuid())
  tokenHash        String?
  expiresAt        DateTime
  acceptedAt       DateTime?
  revokedAt        DateTime?
  invitedByUserId  String

  Workspace        Workspace           @relation(fields: [workspaceId], references: [id], onDelete: Cascade)

  @@index([email])
  @@index([token])
  @@index([workspaceId])
}

model OrganizationSubscription {
  organizationId      String   @id
  planCode            String?
  planTier            String?
  stripeCustomerId    String?
  stripeSubscriptionId String?
  status              String   @default("active")
  renewalAt           DateTime?
  cancelAt            DateTime?
  entitlements        Json     @default("{}")
  createdAt           DateTime @default(now())
  updatedAt           DateTime @updatedAt

  Organization        Organization @relation(fields: [organizationId], references: [id], onDelete: Cascade)
}

model OrganizationSeatAssignment {
  id               String     @id @default(uuid())
  organizationId   String
  userId           String
  seatType         SeatType   @default(FREE)
  status           SeatStatus @default(ACTIVE)
  assignedByUserId String?
  createdAt        DateTime   @default(now())
  updatedAt        DateTime   @updatedAt

  OrgMember        OrgMember? @relation(fields: [organizationId, userId], references: [orgId, userId])

  @@unique([organizationId, userId])
  @@index([organizationId, status])
}

model OrgBalance {
  orgId     String   @id
  balance   Int      @default(0)
  updatedAt DateTime @updatedAt

  Org       Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
}

model OrgCreditTransaction {
  transactionKey    String   @default(uuid())
  createdAt         DateTime @default(now())
  orgId             String
  initiatedByUserId String?
  workspaceId       String?
  amount            Int
  type              CreditTransactionType
  runningBalance    Int?
  isActive          Boolean  @default(true)
  metadata          Json?

  Org               Organization @relation(fields: [orgId], references: [id], onDelete: NoAction)

  @@id(name: "orgCreditTransactionIdentifier", [transactionKey, orgId])
  @@index([orgId, createdAt])
  @@index([initiatedByUserId])
}

model TransferRequest {
  id                       String         @id @default(uuid())
  resourceType             String
  resourceId               String
  sourceOrganizationId     String
  targetOrganizationId     String
  initiatedByUserId        String
  status                   TransferStatus @default(PENDING)
  sourceApprovedByUserId   String?
  targetApprovedByUserId   String?
  completedAt              DateTime?
  reason                   String?
  createdAt                DateTime       @default(now())
  updatedAt                DateTime       @updatedAt

  SourceOrg                Organization   @relation("TransferSource", fields: [sourceOrganizationId], references: [id])
  TargetOrg                Organization   @relation("TransferTarget", fields: [targetOrganizationId], references: [id])

  @@index([sourceOrganizationId])
  @@index([targetOrganizationId])
  @@index([status])
}

model AuditLog {
  id              String   @id @default(uuid())
  organizationId  String?
  workspaceId     String?
  actorUserId     String
  entityType      String
  entityId        String?
  action          String
  beforeJson      Json?
  afterJson       Json?
  correlationId   String?
  createdAt       DateTime @default(now())

  Organization    Organization? @relation(fields: [organizationId], references: [id])

  @@index([organizationId, createdAt])
  @@index([actorUserId, createdAt])
  @@index([entityType, entityId])
}

model IntegrationCredential {
  id              String              @id @default(uuid())
  organizationId  String
  ownerType       CredentialOwnerType
  ownerId         String
  provider        String
  credentialType  String
  displayName     String
  encryptedPayload String
  createdByUserId String
  lastUsedAt      DateTime?
  status          String              @default("active")
  metadata        Json?
  expiresAt       DateTime?
  createdAt       DateTime            @default(now())
  updatedAt       DateTime            @updatedAt

  Organization    Organization        @relation(fields: [organizationId], references: [id], onDelete: Cascade)
  Workspace       Workspace?          @relation("WorkspaceCredentials", fields: [ownerId], references: [id], onDelete: Cascade, map: "IntegrationCredential_workspace_fkey")

  @@index([organizationId, ownerType, provider])
  @@index([ownerId, ownerType])
  @@index([createdByUserId])
}
```

### Renamed Model

```prisma
// RENAMED from UserWorkspace → FileSpace
// Preserves workspace:// URI compatibility at API level
model FileSpace {
  // ... all existing UserWorkspace fields unchanged ...
  // Just the model name changes
}
```

### Nullable Columns on Existing Models

Add to every tenant-bound existing model:
```prisma
  organizationId     String?
  workspaceId        String?
  visibility         ResourceVisibility @default(PRIVATE)
```

Plus the `Workspace` relation:
```prisma
  Workspace          Workspace? @relation(fields: [workspaceId], references: [id], onDelete: SetNull)
```

**Models that must receive these columns:**
- AgentGraph (+ index on `[workspaceId, isActive, id, version]`)
- AgentGraphExecution (+ index on `[workspaceId, isDeleted, createdAt]`)
- ChatSession
- AgentPreset
- LibraryAgent
- LibraryFolder
- IntegrationWebhook
- APIKey (+ `ownerType` field: USER | ORG, optional `workspaceIdRestriction`)
- OAuthApplication (same)
- OAuthAuthorizationCode
- OAuthAccessToken
- OAuthRefreshToken
- StoreListing (add `owningOrgId` keeping existing `owningUserId`)
- StoreListingVersion
- BuilderSearchHistory
- PendingHumanReview

**User model additions:**
```prisma
  OrgMemberships       OrgMember[]
  WorkspaceMemberships WorkspaceMember[]
```

### Design Notes
- `organizationId` is nullable during migration. Becomes NOT NULL in PR18.
- `createdByUserId` is always preserved (audit/attribution) even after org tenancy is canonical.
- `visibility` defaults to `PRIVATE`. Sharing is always explicit.
- Existing actor/audit fields (execution userId, credit userId) are NOT repurposed as ownership.

---

## Part 3: Architecture Contracts

### 3A. Auth & Access Context Contract

**`RequestContext`** is the canonical request-level auth object:

```python
@dataclass(frozen=True)
class RequestContext:
    user_id: str
    org_id: str
    workspace_id: str | None      # None = org-home context
    org_role: OrgMemberRole       # single enum value
    workspace_role: WorkspaceMemberRole | None  # None if no workspace
    seat_status: str              # ACTIVE, INACTIVE, PENDING, NONE
```

**Permission checks use role hierarchy:**
- `OWNER` can do anything an `ADMIN` can do.
- `ADMIN` can do anything a `BILLING_MANAGER` can do.
- Hierarchy: `OWNER > ADMIN > BILLING_MANAGER > MEMBER`.

```python
def has_at_least_role(actual: OrgMemberRole, required: OrgMemberRole) -> bool:
    hierarchy = [OrgMemberRole.MEMBER, OrgMemberRole.BILLING_MANAGER, OrgMemberRole.ADMIN, OrgMemberRole.OWNER]
    return hierarchy.index(actual) >= hierarchy.index(required)
```

**Resolution order:**
1. Extract `user_id` from JWT (support `X-Act-As-User-Id` for admin impersonation).
2. Read `X-Org-Id` header → fallback to `active-org-id` cookie → fallback to user's personal org → fail with clear error.
3. Validate user has `OrgMember` row with status=ACTIVE for the resolved org. Reject if not.
4. Read `X-Workspace-Id` header → fallback to `active-workspace-id` cookie → fallback to None (org-home context).
5. If workspace is set, validate it belongs to the active org AND user has `WorkspaceMember` row. If invalid, clear workspace and fall back to org-home; do not reject the entire request.
6. Populate role from membership rows.

**FastAPI dependencies:**
```python
async def get_request_context(request, jwt_payload) -> RequestContext
def requires_org_role(minimum_role: OrgMemberRole) -> Callable
def requires_workspace_role(minimum_role: WorkspaceMemberRole) -> Callable
```

### 3B. Permission Contract

**Org-level** (role hierarchy — each role includes all capabilities of roles below it):

| Action | Minimum Role |
|--------|-------------|
| Delete org | OWNER |
| Transfer ownership | OWNER |
| Rename org / manage aliases | ADMIN |
| Manage members | ADMIN |
| Manage workspaces | ADMIN |
| Create workspaces | BILLING_MANAGER |
| Manage billing / seats | BILLING_MANAGER |
| Publish to store | MEMBER* |
| Create private resources | MEMBER* |
| Share own resources | MEMBER* |
| View org | MEMBER |

\* Requires active seat.

**Workspace-level** (same hierarchy pattern):

| Action | Minimum Role |
|--------|-------------|
| Manage WS members/invites | ADMIN |
| Manage WS settings | ADMIN |
| Manage WS credentials | ADMIN |
| Delete agents | ADMIN |
| View WS spend | BILLING_MANAGER |
| Create agents / use resources | MEMBER* |
| Use credentials | MEMBER* |
| View executions | MEMBER |

\* Requires active seat.

**Seat rules:**
- Active seat required for: creating/running agents, active chat use, credential use in product flows, API key product operations.
- Seat NOT required for: passive membership, billing-only access, invitations, limited admin governance.
- Free seats = active seats with low entitlements.

### 3C. Visibility & Context Contract

**Org-home context** (no workspace selected):
- Shows: `PRIVATE` resources where `createdByUserId = current_user AND workspaceId IS NULL` + `ORG` resources for the active org.
- Does NOT aggregate workspace resources.

**Active-workspace context**:
- Shows: org-home resources PLUS `WORKSPACE` resources for the active workspace + `PRIVATE` resources where `createdByUserId = current_user AND workspaceId = active_workspace`.

**Switching behavior:**
- Org switch → swap ALL visible data, invalidate all tenant-bound caches, restore last-used workspace for new org.
- Workspace switch → swap workspace-scoped data only.
- Frontend must treat `(orgId, workspaceId)` as cache key components.

### 3D. Credential Contract

**Store:** `IntegrationCredential` table replaces `User.integrations` encrypted blob.

**Scopes:**
- `USER`: visible/usable only by the owning user within current org context.
- `WORKSPACE`: usable by workspace members. Managed by workspace admins.
- `ORG`: usable by org members. Managed by org admins (and billing managers, since admin inherits billing manager).

**Resolution (fixed order):**
1. Matching `USER` credential where `createdByUserId = current_user` in active org.
2. Matching `WORKSPACE` credential for active workspace (only if workspace is active).
3. Matching `ORG` credential for active org.

**Transfer rule:** Credentials never move across orgs. Transferred resources require manual reattachment.

### 3E. Marketplace & Slug Contract

- `StoreListing.owningOrgId` replaces `owningUserId`.
- `OrganizationProfile` is the marketplace creator identity.
- URL format: `{org.slug}/{listing.slug}`.
- Old `user_slug/agent_slug` routes → permanent redirects via `OrganizationAlias`.
- Org rename creates alias for old slug. Alias removal is platform-admin-only + audited.
- Workspace resources must be promoted to org-shared before publishing.

### 3F. Billing & Subscription Contract

- Stripe IDs move to `OrganizationSubscription`.
- `OrgBalance` replaces `UserBalance`.
- `OrgCreditTransaction` replaces `CreditTransaction` for new activity.
- Execution billing records preserve `initiatedByUserId` and `workspaceId`.
- Seat assignment managed by org billing managers and above (admin, owner — via role hierarchy).
- Workspace spend derived from workspace-tagged usage.

### 3G. API Key & OAuth Contract

- Add `ownerType` (USER | ORG), `organizationId`, optional `workspaceIdRestriction`.
- `USER`-owned: visible only to owning user in active org.
- `ORG`-owned: visible to appropriate org roles. May restrict to one workspace.
- Token validation checks org ownership + optional workspace restriction.

### 3H. Realtime & Notifications Contract

- Websocket/SSE channel keys must include `orgId` and `workspaceId`.
- Notification routing carries org/workspace context. No cross-org leakage.
- Background jobs carry org/workspace IDs.
- Context switch re-subscribes to new channels.

### 3I. File-Space Contract

- `UserWorkspace` renamed to `FileSpace` in PR1.
- `workspace://` URI compatibility preserved at API level.
- Add `organizationId`, `workspaceId`, `visibility` to file records.
- Files can be `PRIVATE`, `WORKSPACE`-shared, or `ORG`-reachable.

---

## Part 4: Unified PR Sequence (19 PRs)

### PR1: Schema Foundation + Tenant Context Plumbing
**What**: Add all new enums, models, and nullable tenancy columns to `schema.prisma`. Rename `UserWorkspace` → `FileSpace`. Add `X-Org-Id`/`X-Workspace-Id` header constants, active-context cookies, frontend proxy header forwarding, and the backend `RequestContext` dependency shape. No business behavior changes.

**Files to create/modify:**
- `autogpt_platform/backend/schema.prisma` — all new models + nullable columns + FileSpace rename
- `autogpt_platform/backend/migrations/{ts}_add_org_workspace_tables/migration.sql` (auto-generated)
- `autogpt_libs/autogpt_libs/auth/models.py` — add `RequestContext`, `OrgMemberRole`, `WorkspaceMemberRole`
- `autogpt_libs/autogpt_libs/auth/permissions.py` (new) — role hierarchy check, `OrgAction`/`WorkspaceAction` enums, permission maps
- `autogpt_libs/autogpt_libs/auth/dependencies.py` — add `get_request_context`, `requires_org_role`, `requires_workspace_role` + header/cookie constants
- `autogpt_libs/autogpt_libs/auth/__init__.py` — export new symbols
- `frontend/src/app/api/mutators/custom-mutator.ts` — inject `X-Org-Id`/`X-Workspace-Id` headers from localStorage
- All existing code referencing `UserWorkspace` — update to `FileSpace`

**Exit criteria**: Schema compiles, migrations run, headers/cookies round-trip through client→SSR→proxy→backend in tests. FileSpace rename complete. No product routes use new context for authorization yet. All existing tests pass.

**Tests:**
- Migration runs cleanly on empty and populated test DBs
- `permissions_test.py` — role hierarchy checks, every action × role combination
- `dependencies_test.py` — `get_request_context` with/without headers, personal org fallback, non-member rejection, impersonation + tenant context
- FileSpace rename doesn't break existing file operations
- Existing test suite passes unchanged

---

### PR2: Personal Org Bootstrap Migration
**What**: Backfill one org per existing user. Create owner membership, org profile, default workspace, default workspace membership, initial seat assignment, org balance from user balance, org aliases for published agents, and set `organizationId`/`workspaceId` on every existing tenant-bound row with `PRIVATE` visibility.

**Files to create/modify:**
- `backend/data/org_migration.py` (new) — idempotent migration functions
- `backend/api/rest_api.py` — add migration call in lifespan

**Exit criteria**: Migration is idempotent, emits auditable counts, every tenant-bound row has `organizationId` after backfill. All existing flows still work with legacy reads.

**Tests:**
- Slug from profile username, fallback to sanitized id
- Slug collision → numeric suffix + alias created
- Idempotency — run twice, no duplicates
- Every tenant-bound model gets workspaceId
- OrgBalance + OrgCreditTransaction created
- StoreListing gets owningOrgId + aliases created

---

### PR3: Dual-Write Create/Update Paths
**What**: Update all create/update codepaths to write `organizationId`, `workspaceId`, `createdByUserId`, and `visibility` alongside legacy fields. Keep reads legacy.

**Files to modify**: `graph.py`, `execution.py`, chat routes, library routes, integration routes, `v1.py`, all other create/update paths for tenant-bound models.

**Exit criteria**: All new writes are tenancy-complete. Regression tests pass under legacy reads.

**Tests**: Verify tenancy fields populated on new records for each model.

---

### PR4: Org CRUD API
**What**: Org management endpoints.

**Endpoints:**
```
POST   /api/orgs                              — create org
GET    /api/orgs                              — list user's orgs
GET    /api/orgs/{orgId}                      — get org details
PATCH  /api/orgs/{orgId}                      — update org (ADMIN+)
DELETE /api/orgs/{orgId}                      — delete org (OWNER, not personal)
POST   /api/orgs/{orgId}/convert              — personal→team (OWNER)
GET    /api/orgs/{orgId}/members              — list members
POST   /api/orgs/{orgId}/members              — add member (ADMIN+)
PATCH  /api/orgs/{orgId}/members/{uid}        — change role (ADMIN+)
DELETE /api/orgs/{orgId}/members/{uid}        — remove member (ADMIN+, not owner)
POST   /api/orgs/{orgId}/transfer-ownership   — transfer (OWNER)
GET    /api/orgs/{orgId}/aliases              — list aliases
POST   /api/orgs/{orgId}/aliases              — create alias (ADMIN+)
```

**New files:** `backend/api/features/orgs/routes.py`, `model.py`, `db.py`

**Exit criteria**: Full org lifecycle end-to-end. Adding member auto-joins default workspace.

**Tests**: Create, duplicate slug, list, non-member 403, update by role, delete personal fails, convert, add/remove member cascade, role changes, ownership transfer, alias CRUD.

---

### PR5: Workspace CRUD API
**What**: Workspace management within orgs.

**Endpoints:**
```
POST   /api/orgs/{orgId}/workspaces                      — create (BILLING_MANAGER+)
GET    /api/orgs/{orgId}/workspaces                      — list
GET    /api/orgs/{orgId}/workspaces/{wsId}               — details
PATCH  /api/orgs/{orgId}/workspaces/{wsId}               — update (WS ADMIN)
DELETE /api/orgs/{orgId}/workspaces/{wsId}               — delete (ADMIN+, not default)
POST   /api/orgs/{orgId}/workspaces/{wsId}/join          — self-join OPEN
POST   /api/orgs/{orgId}/workspaces/{wsId}/leave         — leave (not default)
GET    /api/orgs/{orgId}/workspaces/{wsId}/members       — list
POST   /api/orgs/{orgId}/workspaces/{wsId}/members       — add (WS ADMIN)
PATCH  /api/orgs/{orgId}/workspaces/{wsId}/members/{uid} — change role
DELETE /api/orgs/{orgId}/workspaces/{wsId}/members/{uid} — remove
```

**Exit criteria**: Full workspace lifecycle. OPEN self-join and PRIVATE invite-only both work.

**Tests**: Create open/private, join open, join private 403, delete default fails, creator becomes admin, leave, member CRUD.

---

### PR6: Invitation API
**What**: Email-based invitation flow for orgs + separate workspace invites for private workspaces.

**Org invitation endpoints:**
```
POST   /api/orgs/{orgId}/invitations          — create (ADMIN+)
GET    /api/orgs/{orgId}/invitations          — list pending (ADMIN+)
DELETE /api/orgs/{orgId}/invitations/{id}     — revoke (ADMIN+)
POST   /api/invitations/{token}/accept        — accept
POST   /api/invitations/{token}/decline       — decline
GET    /api/invitations/pending               — list for current user
```

**Workspace invitation endpoints:**
```
POST   /api/workspaces/{wsId}/invitations     — create (WS ADMIN)
GET    /api/workspaces/{wsId}/invitations     — list pending (WS ADMIN)
DELETE /api/workspaces/{wsId}/invitations/{id} — revoke (WS ADMIN)
POST   /api/workspace-invitations/{token}/accept  — accept
POST   /api/workspace-invitations/{token}/decline — decline
```

**Exit criteria**: Full invitation lifecycle for both org and workspace invites.

**Tests**: Create, non-admin 403, accept, expired 400, adds to workspaces, decline, revoke, list pending, duplicate email.

---

### PR7: Scoped Credential Store
**What**: Replace `User.integrations` blob with `IntegrationCredential` table. Migrate existing credentials. Dual-read (new table primary, blob fallback).

**Runtime resolution**: USER → WORKSPACE → ORG.

**Exit criteria**: Credentials work end-to-end from new table. Blob fallback remains as safety net.

**Tests**: Migration from blob, resolution at each scope, fallback chain, workspace member use vs manage, admin manage, org-home skips workspace creds.

---

### PR8: Resource Scoping — Graphs, Library, Presets, Webhooks
**What**: Cut to `RequestContext`, enforce same-org constraints, add visibility transitions, resolve scoped credentials.

**Exit criteria**: Agents can be private/workspace-shared/org-shared. Cross-org forbidden. Library and presets obey active org/workspace.

**Tests**: Workspace isolation, visibility transitions (PRIVATE→WORKSPACE→ORG→PRIVATE), cross-org access forbidden, preset uses scoped credentials.

---

### PR9: Chats and Executions
**What**: Tenantize chat sessions and graph executions. Private by default, shareable within org/workspace. Preserve acting-user attribution.

**Exit criteria**: Switching org/workspace swaps visible chats/runs. No cross-org leakage.

**Tests**: Chat/execution workspace isolation, sharing within org, attribution preserved.

---

### PR10: Webhooks, Human Review, Background Execution Context
**What**: Tenantize webhooks and review queues. Background jobs carry org/workspace IDs. Scoped credential resolution.

**Exit criteria**: Webhook-triggered and queued operations resolve same tenant context as interactive operations.

**Tests**: Webhook tenant context, background job carries org/workspace, scoped credential resolution.

---

### PR11: Realtime and Notifications
**What**: Re-key websocket channels, SSE streams, notification routing by org/workspace.

**Exit criteria**: Context switching doesn't leak stale events. Notification batches are tenant-correct.

**Tests**: Websocket/SSE isolation by org/workspace, no stale events after context switch.

---

### PR12: File-Space Shared Files
**What**: Add org/workspace ownership to `FileSpace` records. Support workspace-shared file access. Preserve `workspace://` URI compatibility.

**Exit criteria**: Shared files work inside workspaces. Existing file refs remain valid.

**Tests**: URI compatibility, shared workspace files, file visibility transitions.

---

### PR13: Billing, Credits, Subscriptions, and Seats
**What**: Move Stripe/balance/credit state to org scope. Add seat assignment flows. Enforce seat-gated active use.

**New endpoints:**
```
GET    /api/orgs/{orgId}/seats
POST   /api/orgs/{orgId}/seats/assign
POST   /api/orgs/{orgId}/seats/unassign
GET    /api/orgs/{orgId}/billing
PATCH  /api/orgs/{orgId}/billing
```

**Exit criteria**: Billing managers can manage finance/seats. Seatless members blocked from active use. Free seats enforce low limits.

**Tests**: Org credit spend/top-up, seat assignment, seatless blocked, free seat limits, billing manager permissions.

---

### PR14: API Keys and OAuth Tenancy
**What**: Add org-owned and user-owned API/OAuth assets with optional workspace restriction.

**Exit criteria**: Org automations work. User-private keys stay private. Invalid context rejected.

**Tests**: User-owned vs org-owned, workspace restriction, token validates org context, invalid context rejected.

---

### PR15: Marketplace Org Ownership
**What**: Switch store listings & creator identity to org ownership. Promotion-before-publish. Migrate creator pages. Permanent aliases.

**New endpoints:**
```
POST   /api/graphs/{graphId}/copy       — copy within org
POST   /api/graphs/{graphId}/fork       — fork across orgs
POST   /api/graphs/{graphId}/transfer   — admin transfer
```

**Exit criteria**: New publishes org-owned. Old URLs redirect. Creator pages from org profile.

**Tests**: Publish under org, org slug URLs, workspace requires promotion, legacy redirects, copy/fork.

---

### PR16: Transfer Workflows + Admin Alias Management
**What**: Transfer request/approval/execute flows. Admin alias inspection/removal pages.

**New endpoints:**
```
POST   /api/transfers
GET    /api/transfers
POST   /api/transfers/{id}/approve
POST   /api/transfers/{id}/reject
POST   /api/transfers/{id}/execute
```

**Exit criteria**: Direct transfer for dual-admins. Requested transfers require approval. Aliases auditable. No secret transfer.

**Tests**: Direct transfer, requested transfer approval, credentials not moved, admin alias removal, audit logs.

---

### PR17: Frontend — Full Org/Workspace UI
**What**: State management, header injection, context provider, org switcher, workspace picker, org settings pages, invitation pages, modified existing pages.

**New files:**
- `frontend/src/services/org-workspace/store.ts` — Zustand store
- `frontend/src/providers/org-workspace/OrgWorkspaceProvider.tsx`
- `frontend/src/components/layout/Navbar/components/OrgWorkspaceSwitcher/`
- `frontend/src/app/(platform)/org/` — settings, members, billing, seats, workspaces pages
- `frontend/src/app/(platform)/org/invite/[token]/page.tsx`

**Modified pages:**
- `custom-mutator.ts` — inject org/workspace headers
- `local-storage.ts` — add ACTIVE_ORG, ACTIVE_WORKSPACE keys
- `providers.tsx` — add OrgWorkspaceProvider
- `Navbar.tsx` — add switcher
- Integrations page — scope sections (USER/WORKSPACE/ORG)
- Publish agent modal — "Publish as" org selector
- Credits page — redirect to org billing for non-personal orgs

**Exit criteria**: Switching org/workspace updates all visible data. No stale foreign data. Org settings functional.

**Tests**: Storybook stories, store unit tests, Playwright E2E for create org, invite, switch, publish.

---

### PR18: Tenancy-First Read Cutover
**What**: Make `organizationId` NOT NULL on all tenant-bound models. Switch all read paths to tenancy-first logic under feature flags. Retain compatibility fallback. Measure mismatch telemetry.

**Exit criteria**: Primary reads no longer depend on user-only ownership semantics. Mismatch telemetry active.

**Tests**: All reads tenancy-first, constraint tightening migration passes, mismatch telemetry captures discrepancies.

---

### PR19: Legacy Cleanup
**What**: Remove `User.integrations` blob reads. Remove obsolete user-only ownership checks. Remove dead fallback code. Rebuild analytics/search/materialized views on org/workspace ownership. Finalize docs/tests.

**Exit criteria**: One canonical tenancy model. All tests pass under tenancy-first mode. No legacy fallbacks remain.

**Tests**: No legacy blob dependency, search results org-scoped, analytics org-scoped, full regression suite.

---

## Part 5: Test Plan, Feature Flags, Migration Rules & Defaults

### Test Strategy

**Coverage requirement**: Near 100% branch coverage on all new/modified code. The previous invite system was vibe-coded, untested, and reverted. That must not repeat.

**Backend**: Integration tests with real DB — NOT mocks. Use existing `mock_jwt_user`/`mock_jwt_admin` fixtures. Colocate tests (`routes.py` → `routes_test.py`). Every PR touching schema/ownership adds migration verification tests.

**Frontend**: Storybook stories for all new components. Playwright E2E for critical flows. Run: `pnpm format && pnpm lint && pnpm types`.

**Test categories:**

| Category | What to test |
|----------|-------------|
| Migration | Org bootstrap, slug collisions, alias generation, idempotency, full-row backfill |
| Auth | Missing/invalid headers, cookie/header precedence, impersonation + tenant context, invalid workspace fallback |
| Permissions | Every org role × action using hierarchy, every workspace role × action |
| Seats | Paid, free, seatless, billing-only, seat-gated actions |
| Visibility | Org-home, active-workspace, private, workspace-shared, org-shared |
| Credentials | Migration, CRUD, visibility, precedence, runtime resolution, transfer non-support |
| Agent/Library | Private/shared transitions, no cross-org sharing, workspace/org visibility |
| Chat/Run | Tenant filtering, sharing, attribution, no cross-org leakage |
| Webhook/Background | Tenant-correct context, credential resolution |
| Files | Existing ref compatibility, workspace-sharing |
| Billing | Org finance, seat assignment, free seat limits, workspace attribution |
| API/OAuth | Owner types, workspace restrictions, token claims, invalid context |
| Marketplace | Org creator identity, promotion-before-publish, alias redirects, admin alias removal |
| Realtime | Websocket/SSE isolation, no stale events |
| Transfer | Direct, requested, secret non-movement, audit logs |
| Frontend | Switchers, cache invalidation, route guards, visible-data swaps |

---

### Feature Flags

| Flag | Purpose | Introduced | Removed |
|------|---------|-----------|---------|
| `ENABLE_TENANT_CONTEXT` | RequestContext resolution | PR1 | PR19 |
| `ENABLE_TENANCY_DUAL_WRITE` | Writes include tenancy fields | PR3 | PR19 |
| `ENABLE_SCOPED_CREDENTIALS` | Read from IntegrationCredential | PR7 | PR19 |
| `ENABLE_ORG_WORKSPACES` | Workspace UI and API | PR5 | PR19 |
| `ENABLE_ORG_BILLING` | Org-level billing | PR13 | PR19 |
| `ENABLE_TENANT_API_OWNERSHIP` | API keys/OAuth org ownership | PR14 | PR19 |
| `ENABLE_ORG_MARKETPLACE` | Marketplace uses org identity | PR15 | PR19 |
| `ENABLE_TENANT_REALTIME` | Realtime channels tenant-keyed | PR11 | PR19 |
| `ENABLE_TENANCY_READS` | Primary reads tenancy-first | PR18 | PR19 |
| `ENABLE_LEGACY_TENANCY_FALLBACK` | Legacy read fallback | PR3 | PR19 |

---

### Migration Execution Rules

1. Additive schema → backfill → dual-write → dual-read → read cutover → cleanup.
2. Do NOT remove legacy fields before all reads are tenancy-first.
3. Personal org creation before any read cutover.
4. Backfill sets `workspaceId` to default workspace (not null) for all migrated resources.
5. Existing private resources → org-owned, remain `PRIVATE`.
6. Published agents → org-owned + migration aliases.
7. Webhooks, presets, chats, runs, files, credentials → remain `PRIVATE`.
8. API keys/OAuth → user-owned within personal org.
9. Billing state → org-owned in personal org.
10. Migration scripts MUST be idempotent.
11. Measure mismatch counts before removing compatibility code.
12. Transfers never move credentials or billing history across orgs.

---

### Per-PR Acceptance Rules

- Independently reviewable and mergeable.
- App remains runnable (flags/compatibility where needed).
- No big-bang merge required.
- Tests updated for affected subsystem.
- Schema/ownership PRs include migration verification tests.
- Update "Current step" in this doc after merge.

---

### API Route Summary

**Org management:**
```
POST   /api/orgs
GET    /api/orgs
GET    /api/orgs/{orgId}
PATCH  /api/orgs/{orgId}
DELETE /api/orgs/{orgId}
POST   /api/orgs/{orgId}/convert
GET    /api/orgs/{orgId}/members
POST   /api/orgs/{orgId}/members
PATCH  /api/orgs/{orgId}/members/{uid}
DELETE /api/orgs/{orgId}/members/{uid}
POST   /api/orgs/{orgId}/transfer-ownership
GET    /api/orgs/{orgId}/aliases
POST   /api/orgs/{orgId}/aliases
GET    /api/orgs/{orgId}/seats
POST   /api/orgs/{orgId}/seats/assign
POST   /api/orgs/{orgId}/seats/unassign
GET    /api/orgs/{orgId}/billing
PATCH  /api/orgs/{orgId}/billing
POST   /api/orgs/{orgId}/invitations
GET    /api/orgs/{orgId}/invitations
DELETE /api/orgs/{orgId}/invitations/{id}
POST   /api/invitations/{token}/accept
POST   /api/invitations/{token}/decline
GET    /api/invitations/pending
```

**Workspace management:**
```
POST   /api/orgs/{orgId}/workspaces
GET    /api/orgs/{orgId}/workspaces
GET    /api/orgs/{orgId}/workspaces/{wsId}
PATCH  /api/orgs/{orgId}/workspaces/{wsId}
DELETE /api/orgs/{orgId}/workspaces/{wsId}
POST   /api/orgs/{orgId}/workspaces/{wsId}/join
POST   /api/orgs/{orgId}/workspaces/{wsId}/leave
GET    /api/orgs/{orgId}/workspaces/{wsId}/members
POST   /api/orgs/{orgId}/workspaces/{wsId}/members
PATCH  /api/orgs/{orgId}/workspaces/{wsId}/members/{uid}
DELETE /api/orgs/{orgId}/workspaces/{wsId}/members/{uid}
POST   /api/workspaces/{wsId}/invitations
GET    /api/workspaces/{wsId}/invitations
DELETE /api/workspaces/{wsId}/invitations/{id}
POST   /api/workspace-invitations/{token}/accept
POST   /api/workspace-invitations/{token}/decline
```

**Transfers:**
```
POST   /api/transfers
GET    /api/transfers
POST   /api/transfers/{id}/approve
POST   /api/transfers/{id}/reject
POST   /api/transfers/{id}/execute
```

**Portability:**
```
POST   /api/graphs/{graphId}/copy
POST   /api/graphs/{graphId}/fork
POST   /api/graphs/{graphId}/transfer
```

---

### Frontend Routes

```
/org/settings
/org/members
/org/billing
/org/seats
/org/workspaces
/org/workspaces/[id]/settings
/org/workspaces/[id]/members
/org/invite/[token]
```

---

### Explicit Defaults For Ambiguity

- Prefer additive schema over destructive during rollout.
- Prefer path-stable APIs with new tenant context over route churn.
- Prefer explicit sharing over implicit sharing from current workspace.
- Prefer org-home as post-login landing.
- Prefer org-local workspace slugs + mutable display names.
- Prefer preserving IDs during transfer, never move secrets automatically.
- Prefer denying access on ambiguous context rather than guessing.
- Prefer keeping legacy compatibility behind flags until mismatch telemetry is clean.
- Personal org slug: Profile.username → sanitized User.name → email-local-part → UUID-derived. Collisions get numeric suffixes.
- Active workspace stored per active org for restore-on-switch.
- Sharing requires resource creator to belong to target scope + have active seat.
- Admins manage shared resources at their scope but cannot inspect another user's private resources.
- Only org-shared agents (visibility=ORG) can be published.

### Explicit Non-Goals For V1

- No cross-org sharing.
- No direct workspace-to-public publish without org promotion.
- No automatic credential transfer between orgs.
- No transfer of private chats or historical runs between orgs.
- No workspace-level payment methods separate from org.
- No requirement that every member have a seat.
- No removal of existing file route compatibility until rollout is stable.

---

### Progress Tracking

| PR | Marker |
|----|--------|
| PR1 | New models in `schema.prisma` + `RequestContext` in auth + `FileSpace` rename |
| PR2 | `org_migration.py` exists and has run |
| PR3 | Create paths write `organizationId` + `workspaceId` |
| PR4 | `/api/orgs` routes exist |
| PR5 | Workspace routes under `/api/orgs/{id}/workspaces` |
| PR6 | Invitation routes exist (org + workspace) |
| PR7 | `IntegrationCredential` read in `credentials_store.py` |
| PR8 | Graph queries use `workspaceId` + `visibility` |
| PR9 | Chat/execution queries use workspace scope |
| PR10 | Webhooks carry org/workspace context |
| PR11 | Websocket channels include org/workspace keys |
| PR12 | FileSpace records have org/workspace ownership |
| PR13 | `OrgBalance` in `credit.py` + seat enforcement |
| PR14 | API keys have `ownerType` field |
| PR15 | `StoreListing.owningOrgId` is primary owner |
| PR16 | Transfer routes exist |
| PR17 | `useOrgWorkspaceStore` + `OrgWorkspaceSwitcher` in frontend |
| PR18 | `organizationId` NOT NULL + reads tenancy-first |
| PR19 | No legacy blob reads, no dead fallback code |

---

## Reconciliation Decisions Log

These decisions were made by reviewing the user on each point:

1. **PR count**: 19 PRs. Split final cutover into read cutover (PR18) + legacy cleanup (PR19) for safety.
2. **Role model**: Single enum (`OrgMemberRole`, `WorkspaceMemberRole`). Admin inherits billing manager capabilities. No boolean flags.
3. **Naming**: `Workspace` (clean name for collaborative workspaces). `FileSpace` (renamed from `UserWorkspace`). Rename in PR1.
4. **OrganizationAlias**: Included — required for marketplace URL migration and org renames.
5. **OrganizationProfile**: Separate table from Organization — marketplace identity will evolve independently.
6. **TransferRequest**: Included in v1 — cross-org transfer is a launch requirement.
7. **AuditLog**: Included — multi-tenant systems need audit trails for support and compliance.
8. **WorkspaceInvite**: Separate table from OrgInvitation — different audiences (external vs existing org members), different flows.
9. **Credential naming**: `IntegrationCredential` — describes what it stores without implying org-only scope.
10. **Frontend**: One big PR (PR17) — switcher and settings pages require the foundation, splitting adds merge overhead without isolation benefit.
11. **Dual-write**: Dedicated PR3 — one sweep ensures nothing missed, easy to revert if bugs found.
12. **Cutover**: Split into PR18 (read cutover + NOT NULL under flags) and PR19 (legacy cleanup after bake period).
