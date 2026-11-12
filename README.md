# github-access (sandbox pilot)

Who can reach which repository, declared as code. **Dummy data only: this repository is public.**

Each product is one file in `teams/`. Every team in it becomes a GitHub team with exactly that name, and
repositories give access only to those teams. Nobody edits GitHub by hand: anything added there is removed
on the next run.

## How a change happens

1. Someone fills in the form in [access-requests](https://github.com/test-access-sandbox/access-requests/issues/new/choose).
2. A bot edits the product's file and opens a pull request here.
3. `checks` runs the rules and comments what merging would change in GitHub.
4. A product owner (from `CODEOWNERS`) approves and merges.
5. `apply` makes GitHub match the files and tells the requester. It also runs every morning, undoing anything
   changed by hand.

Closing a pull request with a comment declines it: the requester sees the comment.

## Rules the checks enforce

- Team names start with the product name; child teams sit under the product team.
- Only `-admins` has admin (2 people at most); only `-maintainers` has maintain (3 at most).
- Developers never write to the repositories listed under `infrastructure:`.
- Everyone in a team is under `people:` with a work email; every file has an owner, matching `CODEOWNERS`.
- A repository belongs to one product file only.
- Direct grants only for `outside_collaborators`: write at most, with a sponsor, reason and expiry.

## Run it by hand

```
pip install pyyaml
python scripts/access.py check          # the rules
GH_TOKEN=... python scripts/access.py plan    # what apply would change
```
