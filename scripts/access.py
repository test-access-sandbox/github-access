#!/usr/bin/env python3
"""Check the product files in teams/, preview what they would change in GitHub, and apply them.

  python scripts/access.py check                 the rules every file must follow (no GitHub calls)
  python scripts/access.py plan [--comment-pr N] what apply would change; optionally posted on PR N
  python scripts/access.py apply [--notify SHA]  make GitHub match the files; tell the requester
  python scripts/access.py declined N            PR N was closed unmerged; tell the requester why

plan, apply and declined need GH_TOKEN: an installation token of the access App.
"""
import datetime
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
LEVEL = {"read": "pull", "triage": "triage", "write": "push", "maintain": "maintain", "admin": "admin"}
RANK = {"pull": 0, "triage": 1, "push": 2, "maintain": 3, "admin": 4}
WORD = {"pull": "Read", "triage": "Triage", "push": "Write", "maintain": "Maintain", "admin": "Admin"}
MAX_MEMBERS = {"-maintainers": 3, "-admins": 2}
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLAN_MARK = "<!-- access-plan -->"


# ---------------------------------------------------------------- the files

def load():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    files = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "teams", "*.yaml"))):
        with open(path) as f:
            files[os.path.relpath(path, ROOT)] = yaml.safe_load(f) or {}
    return cfg, files


def codeowners():
    """The owners CODEOWNERS gives each path, e.g. {'/teams/demo.yaml': {'edward-obeng'}}."""
    out = {}
    path = os.path.join(ROOT, "CODEOWNERS")
    if os.path.exists(path):
        for line in open(path):
            parts = line.split("#")[0].split()
            if len(parts) > 1:
                out[parts[0]] = {p.lstrip("@") for p in parts[1:]}
    return out


def check(cfg, files):
    """Every rule a product file must follow. Returns a list of problems, empty when all is well."""
    errs, repo_files, team_files = [], {}, {}
    owners_in_codeowners = codeowners()
    for path, d in files.items():
        def e(msg):
            errs.append(f"{path}: {msg}")
        p = d.get("product")
        if not p or os.path.basename(path) != f"{p}.yaml":
            e("`product:` must match the file name")
            continue
        people = d.get("people") or {}
        for login, mail in people.items():
            if not LOGIN.match(str(login)):
                e(f"`{login}` is not a GitHub login")
            if not EMAIL.match(str(mail or "")):
                e(f"{login} needs a work email under people")
        owners = d.get("owners") or []
        if not owners:
            e("needs at least one owner")
        for o in owners:
            if o not in people:
                e(f"owner {o} must be listed under people")
        if set(owners) != owners_in_codeowners.get(f"/teams/{p}.yaml", set()):
            e(f"CODEOWNERS must give /teams/{p}.yaml exactly its owners: {', '.join(owners)}")
        infra = set(d.get("infrastructure") or [])
        teams = d.get("teams") or {}
        if p not in teams:
            e(f"needs the product team `{p}`")
        for name, t in teams.items():
            t = t or {}
            where = f"team {name}"
            if name == cfg["everyone_team"]:
                e(f"{where}: this name is kept for the all-members team")
            if name != p and not name.startswith(p + "-"):
                e(f"{where}: the name must start with `{p}-`")
            if name in team_files:
                e(f"{where}: also declared in {team_files[name]}")
            team_files[name] = path
            parent = t.get("parent")
            if name == p and parent:
                e(f"{where}: the product team has no parent")
            if name != p and parent not in teams:
                e(f"{where}: `parent:` must be a team in this file")
            perm = t.get("permission")
            if perm not in LEVEL:
                e(f"{where}: permission must be one of {', '.join(LEVEL)}")
                continue
            if perm == "admin" and not name.endswith("-admins"):
                e(f"{where}: only an -admins team may have admin")
            if perm == "maintain" and not name.endswith("-maintainers"):
                e(f"{where}: only a -maintainers team may have maintain")
            members = t.get("members") or []
            if len(members) != len(set(members)):
                e(f"{where}: someone is listed twice")
            for m in members:
                if m not in people:
                    e(f"{where}: {m} must be listed under people, with their email")
            for suffix, most in MAX_MEMBERS.items():
                if name.endswith(suffix) and len(members) > most:
                    e(f"{where}: {len(members)} people, at most {most}")
            repos = t.get("repositories") or []
            writes_infra = sorted(infra & set(repos))
            if writes_infra and RANK[LEVEL[perm]] >= RANK["push"] and perm not in ("maintain", "admin"):
                e(f"{where}: developers only read infrastructure ({', '.join(writes_infra)})")
            for r in repos:
                repo_files.setdefault(r, set()).add(path)
        today = datetime.date.today()
        for oc in d.get("outside_collaborators") or []:
            who = f"outside collaborator {oc.get('login')}"
            for key in ("login", "repository", "permission", "sponsor", "reason", "expires"):
                if not oc.get(key):
                    e(f"{who}: needs `{key}`")
            if oc.get("permission") not in ("read", "triage", "write"):
                e(f"{who}: write at most")
            if oc.get("sponsor") and oc["sponsor"] not in people:
                e(f"{who}: the sponsor must be listed under people")
            exp = oc.get("expires")
            if isinstance(exp, datetime.date) and exp < today:
                e(f"{who}: expired on {exp}; renew it or remove the entry")
            if oc.get("repository"):
                repo_files.setdefault(oc["repository"], set()).add(path)
    for r, paths in sorted(repo_files.items()):
        if len(paths) > 1:
            errs.append(f"repository {r} is in more than one product file: {', '.join(sorted(paths))}")
    return errs


def desired(cfg, files, org_members):
    """What GitHub should look like: teams, the repositories the files manage, outside collaborators."""
    teams, managed, outside = {}, set(), {}
    for d in files.values():
        for name, t in d["teams"].items():
            repos = {r: LEVEL[t["permission"]] for r in t.get("repositories") or []}
            teams[name] = {"parent": t.get("parent"), "members": set(t.get("members") or []), "repos": repos}
            managed |= set(repos)
        for oc in d.get("outside_collaborators") or []:
            outside[(oc["repository"], oc["login"])] = LEVEL[oc["permission"]]
            managed.add(oc["repository"])
    teams[cfg["everyone_team"]] = {"parent": None, "members": set(org_members),
                                   "repos": {cfg["request_repo"]: "pull"}}
    return teams, managed, outside


def access_of(files, login):
    """{repo: level} that `login` gets from the files, counting what child teams inherit."""
    out = {}
    for d in files.values():
        teams = d["teams"]
        for name, t in teams.items():
            if login not in (t.get("members") or []):
                continue
            chain, cur = [], name
            while cur:
                chain.append(cur)
                cur = teams[cur].get("parent")
            for tn in chain:
                lvl = LEVEL[teams[tn]["permission"]]
                for r in teams[tn].get("repositories") or []:
                    if RANK[lvl] > RANK.get(out.get(r), -1):
                        out[r] = lvl
    return out


# ---------------------------------------------------------------- GitHub

def call(method, path, body=None, ok=(200, 201, 202, 204)):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "github-access"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as err:
        if err.code in ok:
            return None, err.headers
        raise RuntimeError(f"{method} {path}: HTTP {err.code} {err.read().decode()[:200]}")


def get(path):
    return call("GET", path)[0]


def pages(path):
    out, url = [], API + path + ("&" if "?" in path else "?") + "per_page=100"
    while url:
        data, headers = call("GET", url)
        out += data
        m = re.search(r'<([^>]+)>; rel="next"', headers.get("Link") or "")
        url = m.group(1) if m else None
    return out


def graphql(query, **variables):
    data, _ = call("POST", "/graphql", {"query": query, "variables": variables})
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"])[:300])
    return data["data"]


MEMBERS_Q = """query($org: String!, $slug: String!) { organization(login: $org) { team(slug: $slug) {
  members(first: 100, membership: IMMEDIATE) { edges { role node { login } } } } } }"""


def current(cfg, managed):
    """What GitHub looks like now, for the parts the files manage."""
    org = cfg["org"]
    now = {"members": {m["login"] for m in pages(f"/orgs/{org}/members")},
           "owners": {m["login"] for m in pages(f"/orgs/{org}/members?role=admin")}, "teams": {}, "repos": {},
           "direct": {}, "invites": {}, "existing_repos": {r["name"] for r in pages(f"/orgs/{org}/repos?type=all")}}
    for t in pages(f"/orgs/{org}/teams"):
        team = graphql(MEMBERS_Q, org=org, slug=t["slug"])["organization"]["team"]
        now["teams"][t["slug"]] = {"id": t["id"], "parent": (t.get("parent") or {}).get("slug"),
                                   "members": {e["node"]["login"]: e["role"] for e in team["members"]["edges"]}}
    for r in sorted(managed | {cfg["request_repo"]}):
        if r not in now["existing_repos"]:
            continue
        now["repos"][r] = {t["slug"]: t["permission"] for t in pages(f"/repos/{org}/{r}/teams")}
        if r in managed:
            now["direct"][r] = {c["login"]: c["role_name"] for c in pages(f"/repos/{org}/{r}/collaborators?affiliation=direct")}
            now["invites"][r] = {i["invitee"]["login"]: i["id"] for i in pages(f"/repos/{org}/{r}/invitations")
                                 if i.get("invitee")}
    return now


def plan(cfg, files):
    """The changes that make GitHub match the files, as (description, action) pairs; and notes."""
    org = cfg["org"]
    teams_d, managed, outside = desired(cfg, files, [])
    now = current(cfg, managed)
    teams_d, managed, outside = desired(cfg, files, now["members"])
    steps, notes = [], []
    team_ids = {s: t["id"] for s, t in now["teams"].items()}

    def ancestors(slug):
        out, cur = [], teams_d.get(slug, {}).get("parent")
        while cur:
            out.append(cur)
            cur = teams_d.get(cur, {}).get("parent")
        return out

    # Teams, parents first.
    order = sorted(teams_d, key=lambda s: (len(ancestors(s)), s))
    for slug in order:
        want = teams_d[slug]
        have = now["teams"].get(slug)
        if have is None:
            def create(slug=slug, parent=want["parent"]):
                body = {"name": slug, "privacy": "closed"}
                if parent:
                    body["parent_team_id"] = team_ids[parent]
                team_ids[slug] = call("POST", f"/orgs/{org}/teams", body)[0]["id"]
            steps.append((f"Create team `{slug}`" + (f" under `{want['parent']}`" if want["parent"] else ""), create))
        elif have["parent"] != want["parent"]:
            def reparent(slug=slug, parent=want["parent"]):
                call("PATCH", f"/orgs/{org}/teams/{slug}", {"parent_team_id": team_ids[parent] if parent else None})
            steps.append((f"Move team `{slug}` under `{want['parent']}`", reparent))

    # Members.
    for slug in order:
        want, have = teams_d[slug]["members"], (now["teams"].get(slug) or {}).get("members", {})
        for login in sorted(want - set(have)):
            if login not in now["members"]:
                notes.append(f"@{login} is not a member of {org}, so is not added to `{slug}`. Invite them to the org first.")
                continue
            steps.append((f"Add @{login} to `{slug}`",
                          lambda s=slug, u=login: call("PUT", f"/orgs/{org}/teams/{s}/memberships/{u}", {"role": "member"})))
        for login, role in sorted(have.items()):
            if login not in want:
                steps.append((f"Remove @{login} from `{slug}` (not in the file)",
                              lambda s=slug, u=login: call("DELETE", f"/orgs/{org}/teams/{s}/memberships/{u}")))
            elif role == "MAINTAINER" and login not in now["owners"]:  # GitHub makes org owners maintainers
                steps.append((f"Make @{login} a plain member of `{slug}` (the team-maintainer role is not used)",
                              lambda s=slug, u=login: call("PUT", f"/orgs/{org}/teams/{s}/memberships/{u}", {"role": "member"})))

    # Team access to repositories.
    for r in sorted(managed | {cfg["request_repo"]}):
        if r not in now["existing_repos"]:
            notes.append(f"Repository `{r}` does not exist in {org}.")
            continue
        want = {s: t["repos"][r] for s, t in teams_d.items() if r in t["repos"]}
        have = now["repos"].get(r, {})
        for slug, lvl in sorted(want.items()):
            if have.get(slug) != lvl:
                steps.append((f"Give `{slug}` {WORD[lvl]} on `{r}`" + (f" (now {WORD.get(have[slug], have[slug])})" if slug in have else ""),
                              lambda s=slug, rr=r, p=lvl: call("PUT", f"/orgs/{org}/teams/{s}/repos/{org}/{rr}", {"permission": p})))
        if r not in managed:
            continue  # the request repository: only the all-members grant is ours
        for slug, lvl in sorted(have.items()):
            if slug in want:
                continue
            if slug in teams_d and any(a in want for a in ancestors(slug)):
                continue  # access inherited from a parent team, which is declared
            steps.append((f"Remove `{slug}` from `{r}` (not in any file)",
                          lambda s=slug, rr=r: call("DELETE", f"/orgs/{org}/teams/{s}/repos/{org}/{rr}")))

    # Direct grants: only the outside collaborators in the files.
    for r in sorted(managed):
        if r not in now["existing_repos"]:
            continue
        for login, role in sorted(now["direct"][r].items()):
            want = outside.get((r, login))
            if want is None:
                steps.append((f"Remove the direct grant of @{login} on `{r}` ({role})",
                              lambda rr=r, u=login: call("DELETE", f"/repos/{org}/{rr}/collaborators/{u}")))
            elif role != {"pull": "read", "push": "write"}.get(want, want):
                steps.append((f"Set @{login} to {WORD[want]} on `{r}`",
                              lambda rr=r, u=login, p=want: call("PUT", f"/repos/{org}/{rr}/collaborators/{u}", {"permission": p})))
        for login, inv in sorted(now["invites"][r].items()):
            if (r, login) not in outside:
                steps.append((f"Cancel the invitation of @{login} to `{r}`",
                              lambda rr=r, i=inv: call("DELETE", f"/repos/{org}/{rr}/invitations/{i}")))
        for (rr, login), lvl in sorted(outside.items()):
            if rr == r and login not in now["direct"][r] and login not in now["invites"][r]:
                steps.append((f"Invite outside collaborator @{login} to `{r}` with {WORD[lvl]}",
                              lambda r2=r, u=login, p=lvl: call("PUT", f"/repos/{org}/{r2}/collaborators/{u}", {"permission": p})))

    for slug in sorted(set(now["teams"]) - set(teams_d)):
        notes.append(f"Team `{slug}` is not in any file. It is left alone, but loses any access to the product repositories.")
    return steps, notes


def markdown(steps, notes, title):
    lines = [title, ""]
    lines += [f"- {d}" for d, _ in steps] or ["Nothing: GitHub already matches the files."]
    if notes:
        lines += ["", "**Notes**", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines)


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


def request_of(cfg, pr):
    """The request issue a pull request came from, and who asked: (number, login) or (None, None)."""
    body = pr.get("body") or ""
    m = re.search(rf"Request: {re.escape(cfg['org'])}/{re.escape(cfg['request_repo'])}#(\d+)", body)
    who = re.search(r"Requested by @([A-Za-z0-9-]+)", body)
    return (int(m.group(1)), who.group(1) if who else None) if m else (None, None)


def tell(cfg, number, text, close_as=None, label=None):
    base = f"/repos/{cfg['org']}/{cfg['request_repo']}/issues/{number}"
    call("POST", f"{base}/comments", {"body": text})
    if label:
        call("PUT", f"{base}/labels", {"labels": ["access-request", label]})
    if close_as:
        call("PATCH", base, {"state": "closed", "state_reason": close_as})


# ---------------------------------------------------------------- commands

def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    cfg, files = load()
    errs = check(cfg, files)
    if cmd == "check":
        if errs:
            print("The files break these rules:\n" + "\n".join(f"- {e}" for e in errs))
            summary("### Checks failed\n\n" + "\n".join(f"- {e}" for e in errs))
            sys.exit(1)
        print(f"All {len(files)} product files follow the rules.")
        return
    if errs:
        sys.exit("Fix the files first:\n" + "\n".join(f"- {e}" for e in errs))
    org, repo = cfg["org"], "github-access"

    if cmd == "plan":
        steps, notes = plan(cfg, files)
        text = markdown(steps, notes, "### What merging this changes in GitHub")
        print(text)
        summary(text)
        if "--comment-pr" in args:
            n = args[args.index("--comment-pr") + 1]
            body = f"{PLAN_MARK}\n{text}\n\n<sub>Compared with GitHub as it is now. Updated on every push to this pull request.</sub>"
            mine = [c for c in pages(f"/repos/{org}/{repo}/issues/{n}/comments") if PLAN_MARK in (c.get("body") or "")]
            if mine:
                call("PATCH", f"/repos/{org}/{repo}/issues/comments/{mine[0]['id']}", {"body": body})
            else:
                call("POST", f"/repos/{org}/{repo}/issues/{n}/comments", {"body": body})

    elif cmd == "apply":
        steps, notes = plan(cfg, files)
        done, failed = [], []
        for desc, act in steps:
            try:
                act()
                done.append(desc)
                print(f"done: {desc}")
            except RuntimeError as err:
                failed.append(f"{desc}: {err}")
                print(f"FAILED: {desc}: {err}")
        text = markdown([(d, None) for d in done], notes + [f"Failed: {f}" for f in failed], "### Applied")
        summary(text)
        if "--notify" in args:
            sha = args[args.index("--notify") + 1]
            for pr in get(f"/repos/{org}/{repo}/commits/{sha}/pulls") or []:
                number, login = request_of(cfg, pr)
                if not number:
                    continue
                if failed:
                    tell(cfg, number, f"Approved in {pr['html_url']}, but applying it failed. DevSecOps has been told.")
                    continue
                got = access_of(files, login) if login else {}
                if got:
                    rows = "\n".join(f"| `{r}` | {WORD[l]} |" for r, l in sorted(got.items()))
                    text = (f"Done. @{login}'s access now, through their teams:\n\n| Repository | Access |\n| --- | --- |\n{rows}\n\n"
                            f"Approved in {pr['html_url']}.")
                else:
                    text = f"Done. @{login} no longer has access through any product team. Approved in {pr['html_url']}."
                tell(cfg, number, text, close_as="completed", label="done")
        if failed:
            sys.exit(1)

    elif cmd == "declined":
        n = args[1]
        pr = get(f"/repos/{org}/{repo}/pulls/{n}")
        if pr.get("merged_at"):
            return
        number, _ = request_of(cfg, pr)
        if not number:
            return
        said = [(c["created_at"], c["user"]["login"], c.get("body") or "")
                for c in pages(f"/repos/{org}/{repo}/issues/{n}/comments") + pages(f"/repos/{org}/{repo}/pulls/{n}/reviews")
                if c["user"]["type"] != "Bot" and (c.get("body") or "").strip() and PLAN_MARK not in (c.get("body") or "")]
        if said:
            _, who, text = max(said)
            reason = "\n".join("> " + line for line in text.strip().splitlines())
            msg = f"Declined by @{who} in {pr['html_url']}:\n\n{reason}"
        else:
            msg = f"Declined in {pr['html_url']} with no reason given. Ask the product owner, or open a new request."
        tell(cfg, number, msg, close_as="not_planned", label="declined")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
