---
name: create-milestone
description: "Create a GitHub milestone for an upcoming release. Suggests the next version based on the latest release, gathers all merged PRs and closed issues since that release, presents a draft with two tables (Issues and PRs) for user approval, then creates the milestone and assigns all approved items."
license: Apache-2.0
metadata:
  author: mcp-gateway-registry
  version: "2.0"
---

# Create Milestone Skill

Use this skill when the user wants to plan and create a GitHub milestone for an upcoming release. The skill identifies what has changed since a baseline release, presents a draft for approval, and creates the milestone with all approved items.

Supports creating multiple milestones in sequence (e.g., v1.0.20, v1.0.21) by letting the user choose the baseline and which items belong to which release.

## Input

The skill accepts optional parameters:

```
/create-milestone [VERSION] [DATE] [NOTES]
```

- **VERSION** - Target version (e.g., `v1.0.20`). If not provided, suggest based on latest release.
- **DATE** - Target release date (e.g., `2026-04-21`). If not provided, ask the user.
- **NOTES** - Optional theme or notes (e.g., "security hardening", "federation improvements").

## Output

- A GitHub milestone with description containing two tables (Issues and PRs)
- A local draft file at `.scratchpad/{version}-release-notes.md` with detailed release notes

## Workflow

### Step 1: Discover Releases and Milestones

Gather the current state of releases and milestones:

```bash
# Get recent releases (tags + dates)
gh release list --limit 10 --json tagName,publishedAt,name \
  --jq '.[] | "\(.tagName)\t\(.publishedAt)\t\(.name)"'

# Get existing milestones (to avoid duplicates and show what's already planned)
gh api repos/{owner}/{repo}/milestones --jq '.[] | "\(.number)\t\(.title)\t\(.due_on)\t\(.open_issues)\t\(.closed_issues)"'

# Git tags for reference
git tag --sort=-v:refname | grep '^v[0-9]' | head -10
```

### Step 2: Ask the User What They Want

Present findings and ask the user to specify:

1. **Which version** to create a milestone for (suggest next patch bump from latest release)
2. **Target date** for the release
3. **Baseline release** to diff from -- this is critical for flexibility
4. **Theme or notes** for this release

**Example prompt:**

```
Current state:
- Latest release: v1.0.19 (published April 14, 2026)
- Existing milestones: v1.0.19 (due Apr 14, 30 closed / 3 open)

Suggested next milestone: v1.0.20

Questions:
1. What version? (default: v1.0.20)
2. What is the target release date?
3. Which release should I diff from? (default: v1.0.19, the latest)
   - Other options: v1.0.18, v1.0.17, v1.0.16, ...
4. Any theme or notes? (e.g., "external registry federation")
```

**Baseline flexibility:** The user may say:
- "everything since last release" -- use the latest release as baseline
- "everything since v1.0.17" -- use a specific older release as baseline
- "pick everything since the last known release" -- same as latest release
- "I want to plan two releases: v1.0.20 and v1.0.21" -- run the workflow twice

Wait for the user to respond before proceeding. Do not create anything until the user confirms.

### Step 3: Gather Changes Since Baseline

Once the user confirms version, date, and baseline, gather all changes:

```bash
# Get the baseline release publish timestamp for precise cutoff
RELEASE_DATE=$(gh release view {baseline_version} --json publishedAt --jq '.publishedAt')

# List all PRs merged after the baseline release
gh pr list --state merged --search "merged:>=$RELEASE_DATE" \
  --json number,title,mergedAt,body --limit 100 \
  --jq '.[] | "\(.number)\t\(.mergedAt)\t\(.title)"'

# Get all commits since the baseline tag
git log {baseline_version}..HEAD --oneline

# Also get open PRs that might be targeting this release
gh pr list --state open --json number,title --limit 30 \
  --jq '.[] | "\(.number)\t\(.title)"'
```

### Step 4: Find Corresponding Issues

For each merged PR, extract linked issue numbers from:

1. PR title: patterns like `(#123)`, `(#123, #456)`
2. PR body: patterns like `Fixes #123`, `Closes #123`, `Resolves #123`

```bash
# For each PR, get the body and extract issue references
gh pr view {pr_number} --json title,body \
  --jq '{title: .title, body: (.body // "" | split("\n")[0:10] | join("\n"))}'
```

Build a mapping of PR -> Issues.

### Step 5: Exclude Items Already in Other Milestones

Check which items are already assigned to existing milestones:

```bash
# For each candidate item, check if it already has a milestone
gh api repos/{owner}/{repo}/issues/{number} --jq '.milestone.title // "none"'
```

If an item is already assigned to a different milestone, mark it in the draft so the user can decide. Do not silently skip items.

### Step 6: Present Draft for Approval

Present the draft to the user with two tables and clear guidance on what to include/exclude.

**Start with a default recommendation:** Proactively offer to include all closed issues and merged PRs since the baseline as the starting point. This saves the user from having to say "pick everything." Frame it as:

```
I found {N} merged PRs and {M} related issues since {baseline_version}.

Want me to include all of them as a starting point?
You can then add or remove specific items.
```

If the user says yes (or "pick everything", "include all", "yes go ahead"), proceed with the full list. If they want to be selective, present the tables for cherry-picking.

**Draft format:**

```
Here's what I found since {baseline_version} ({N} PRs merged, {M} issues referenced):

Items already in other milestones are marked with [in {milestone}].

## Issues ({count})

| # | Title | Status | Notes |
|---|-------|--------|-------|
| #814 | Add GitHub private repo auth env vars | Open | |
| #660 | Support authenticated GitHub access | Open | |
| #764 | Disable demo server auto-registration | Closed | [in v1.0.19] |
...

## Pull Requests ({count})

| # | Title | Status | Notes |
|---|-------|--------|-------|
| #782 | feat: GitHub private repo auth | Open | |
| #820 | fix: search pagination | Closed | |
| #815 | Add config propagation check | Closed | [in v1.0.19] |
...

## Open PRs (not yet merged, consider including?)

| # | Title |
|---|-------|
| #825 | feat: webhook notifications |
...

Options:
- "yes, include all" -- add everything listed above (the default)
- "exclude items already in other milestones" -- skip [in vX.X.X] items
- "only include #814, #782, #820" -- cherry-pick specific items
- "exclude #764, #815" -- remove specific items
- "also add #825" -- include an open PR not in the list
```

**Important:** In both tables, always show Open items first, then Closed items. Within each group, sort by number ascending.

Wait for the user to confirm or adjust the list before creating anything.

### Step 7: Create the Milestone

Once approved, create the milestone:

```bash
# First check it doesn't already exist
gh api repos/{owner}/{repo}/milestones --jq '.[] | select(.title == "{version}") | .number'

# Create the milestone
gh api repos/{owner}/{repo}/milestones \
  -f title="{version}" \
  -f due_on="{date}T07:00:00Z" \
  -f description="$DESCRIPTION"
```

The description should contain two markdown tables (Issues and PRs) with clickable links. Format:

```markdown
Release {version} - targeting {day_of_week} {month} {day}, {year}.

{User's theme/notes if provided}

## Issues

| # | Title | Status |
|---|-------|--------|
| [#{num}](https://github.com/{owner}/{repo}/issues/{num}) | {title} | {status} |

## Pull Requests

| # | Title | Status |
|---|-------|--------|
| [#{num}](https://github.com/{owner}/{repo}/pull/{num}) | {title} | {status} |
```

**Table sort order:** Open items first, then Closed items. Within each group, sort by number ascending.

### Step 8: Assign Items to the Milestone

Add all approved PRs and issues to the milestone:

```bash
# Get milestone number from creation response
MILESTONE_NUMBER={number from Step 7 response}

# Add each item (use --field for integer type)
for item in {list_of_numbers}; do
  gh api repos/{owner}/{repo}/issues/$item \
    --method PATCH --field milestone=$MILESTONE_NUMBER --silent
done
```

**Note:** If an item is being moved from another milestone, `--field milestone=N` will reassign it. The user must have approved this in Step 6.

### Step 9: Create Local Release Notes Draft

Create a detailed release notes draft at `.scratchpad/{version}-release-notes.md` containing:

- Highlights section listing major features
- Breaking changes (if any)
- Tables for: New Features, Bug Fixes, Infrastructure, Documentation, Dependency Updates
- New environment variables introduced
- Open items that must close before release
- Stats (total PRs, issues closed/open, contributors)

This file serves as the starting point for the actual release notes when the release is cut (using the `/release-notes` skill).

### Step 10: Present Summary and Ask About Next Milestone

After creating everything, present:

1. Link to the milestone on GitHub
2. Stats: total items, open vs closed
3. Path to the local release notes draft
4. Reminder of open items that need to close before release

Then ask:

```
Milestone {version} created.

Would you like to create another milestone? For example:
- "create v1.0.21 targeting April 21" -- I'll use {version} as the new baseline
- "no, I'm done" -- we're finished
```

This enables the user to plan multiple sequential releases in one session. When creating the next milestone, the baseline automatically shifts to the version just created, and items already assigned to previous milestones are flagged.

## Multiple Milestone Planning

When the user wants to plan multiple releases at once (e.g., "I want to plan v1.0.20 and v1.0.21"):

1. **First milestone:** Run the full workflow above for the first version.
2. **Second milestone:** Use the first version as the new baseline. When gathering changes:
   - Items already assigned to the first milestone are shown with `[in {first_version}]`
   - The user picks from the remaining items plus any new open PRs/issues
3. **Repeat** for additional milestones as needed.

**Example flow:**

```
User: I want to plan v1.0.20 for Monday and v1.0.21 for next Friday

Step 1: Create v1.0.20 milestone
  - Baseline: v1.0.19 (latest release)
  - User picks items for v1.0.20

Step 2: Create v1.0.21 milestone
  - Baseline: v1.0.19 (same release baseline, but now items in v1.0.20 are flagged)
  - User picks remaining items + new items for v1.0.21
```

## Important Rules

- **Always ask the user to confirm** version, date, and baseline before creating anything.
- **Always present the draft** and wait for approval before creating the milestone.
- **Always ask which baseline to diff from.** Default to the latest release, but let the user choose an older one.
- **Never create a milestone that already exists.** Check first. If it exists, ask the user if they want to update it or choose a different version.
- **Never silently skip items.** If an item is in another milestone, show it and let the user decide.
- **Never include emojis** in any output or milestone descriptions.
- **Table sort order** is always: Open items first, then Closed items. Within each status group, sort by number ascending.
- **Two separate tables** are required: one for Issues, one for Pull Requests.
- **Clickable links** in the milestone description must use full URLs, not shorthand `#123` (GitHub does not auto-link in milestone descriptions).
- **Use `--field` (not `-f`)** when setting the milestone number on issues/PRs, since it must be an integer.
- **After creating a milestone, offer to create the next one.** This supports sequential release planning.

## Example Usage

### Minimal input (suggest everything)

```
User: /create-milestone
```

Response:
```
Current state:
- Latest release: v1.0.19 (published April 14, 2026)
- Existing milestones: v1.0.19 (due Apr 14, 30 closed / 3 open)

Suggested next milestone: v1.0.20

1. What version? (default: v1.0.20)
2. Target release date?
3. Diff from which release? (default: v1.0.19)
4. Theme or notes?
```

### With version and date

```
User: /create-milestone v1.0.20 2026-04-21
```

Response:
```
I'll create a v1.0.20 milestone targeting Monday April 21, 2026.
Diffing from v1.0.19 (latest release, published April 14).

Let me gather all changes since v1.0.19...

[presents draft tables for approval]
```

### Pick everything since a specific older release

```
User: /create-milestone v1.0.20 2026-04-21
User (in response to baseline question): everything since v1.0.17
```

Response:
```
Diffing from v1.0.17 (published March 31).
Note: This spans v1.0.18 and v1.0.19 -- items already in those milestones will be flagged.

[presents draft tables with [in v1.0.18] and [in v1.0.19] annotations]
```

### Plan two releases at once

```
User: I want to plan v1.0.20 for Monday and v1.0.21 for next Friday
```

Response:
```
I'll plan both milestones. Starting with v1.0.20.

[runs full workflow for v1.0.20, then asks about v1.0.21]
```

### Cherry-pick specific items

```
User: only include #820, #821, #814, and #782
```

Response:
```
Got it. Creating milestone with just those 4 items.
[creates milestone with only the specified items]
```

## Error Handling

- **Milestone already exists**: Tell the user and ask if they want to update it, add more items to it, or choose a different version.
- **No changes found since baseline**: Tell the user there are no merged PRs since the baseline release. Ask if they want to create an empty milestone for tracking open issues/PRs, or choose an older baseline.
- **Item already in another milestone**: Show it in the draft with `[in {milestone}]` annotation. If the user includes it, reassign it to the new milestone.
- **gh CLI not authenticated**: Tell the user to run `gh auth login`.
