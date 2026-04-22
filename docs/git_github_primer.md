# Git & GitHub Primer — What We Did, and Why

A quick reference so you understand the tools you just set up. Keep this handy for the first few weeks; by Module 2 it'll be second nature.

---

## The cast of characters

| Thing | What it is | Where it lives |
|---|---|---|
| **Git** | The tool/program that tracks changes to files | On your laptop |
| **GitHub** | A website that hosts Git repositories | In the cloud |
| **Repository (repo)** | A project folder that Git tracks | Exists in both places |
| **Clone** | A downloaded copy of a GitHub repo | On your laptop |
| **SSH key** | Digital ID card proving your laptop is yours | Split: private on laptop, public on GitHub |

Think of it like Google Docs for code, except Git is smarter: it remembers every change ever made and lets you rewind.

---

## What actually happened on your laptop

You now have **three layers** working together:

```
┌─────────────────────────────────────────────────────┐
│  GITHUB (cloud)                                      │
│  github.com/yourname/retail-data-platform            │
│  - Public, visible to recruiters                     │
│  - Backup if your laptop dies                        │
└─────────────────────────────────────────────────────┘
            ↑ git push          ↓ git pull
┌─────────────────────────────────────────────────────┐
│  YOUR LAPTOP — the cloned repo                       │
│  C:\Projects\retail-data-platform\                   │
│  - The working copy you actually edit                │
│  - Claude Code reads/writes files here               │
└─────────────────────────────────────────────────────┘
            ↑ git add + git commit
┌─────────────────────────────────────────────────────┐
│  YOUR LAPTOP — files as you edit them                │
│  (unsaved / uncommitted changes)                     │
└─────────────────────────────────────────────────────┘
```

Changes flow **upward**. You edit → commit → push.

---

## Step-by-step recap of what you just did

### 1. Installed Git
The program that tracks file history and talks to GitHub.
```
git --version
```
→ Confirms Git is available.

### 2. Told Git who you are
Every change you make gets stamped with your name + email. One-time setup.
```
git config --global user.name "Satyaki Guha"
git config --global user.email "you@example.com"
```

### 3. Created an SSH key pair
Think "puzzle pieces". Private half stays on your laptop; public half goes to GitHub. They match → GitHub trusts your laptop.
```
ssh-keygen -t ed25519 -C "you@example.com"
```
Files created in `C:\Users\YourName\.ssh\`:
- `id_ed25519` → **private key, never share this**
- `id_ed25519.pub` → public key, safe to share

### 4. Gave GitHub your public key
Pasted the contents of `id_ed25519.pub` into GitHub → Settings → SSH and GPG keys. Now GitHub recognizes your laptop.

### 5. Verified the handshake
```
ssh -T git@github.com
```
→ "Hi yourname!" means it worked. You'll never need to prove your identity again from this laptop.

### 6. Created an empty repo on GitHub
On github.com → New repository → named it, added README + .gitignore + LICENSE. This is the cloud-side project container.

### 7. Cloned it to your laptop
```
cd C:\Projects
git clone git@github.com:yourname/retail-data-platform.git
```
→ Downloads the repo to `C:\Projects\retail-data-platform\`. This folder is now "connected" to GitHub — Git knows where to push updates.

### 8. Test push to prove the loop works
```
git add README.md          ← stage the change for commit
git commit -m "message"    ← save a snapshot locally
git push                   ← upload snapshots to GitHub
```
You saw your README update on github.com → full loop verified.

---

## The 5 Git commands you'll use 95% of the time

These four are your bread and butter:

| Command | What it does | When |
|---|---|---|
| `git status` | "What's changed? What's staged?" | Anytime you're unsure |
| `git add <file>` or `git add .` | Stage changes to be committed | Before committing |
| `git commit -m "message"` | Save a snapshot locally with a label | After finishing a logical chunk of work |
| `git push` | Upload your commits to GitHub | Once you want it backed up / visible |
| `git pull` | Download any updates from GitHub | If working on multiple machines, or rarely for solo work |

### A typical workflow

You finish writing the POS simulator's main loop. You'd do:

```
git status                           # see what changed
git add pos_simulator/main.py        # stage that file
git commit -m "Add main event loop for POS simulator"
git push                             # send to GitHub
```

**Rule of thumb:** commit small, commit often. A commit per logical change ("added this function", "fixed that bug"). Not one giant commit at the end of the day.

---

## Key concepts explained like you're not a software engineer

**Working tree** — the actual files as they exist on your disk right now. This includes any half-finished edits.

**Staging area** — the "about to commit" pile. You use `git add` to put files here. Gives you fine-grained control: "commit these 3 files but not that 4th one yet."

**Commit** — a permanent snapshot. Every commit has a unique ID (a long hash like `a3f9b2c`) and a message. You can always rewind to any past commit.

**Branch** — parallel universes of your code. You're on `main` right now, the default. Later, for experimental work, you'd create a `feature/something` branch, try stuff, and merge back if it works. We're not using branches yet — it's fine to live on `main` as a solo dev for a while.

**Push vs Pull** — push = upload to GitHub. Pull = download from GitHub. You'll push a lot, pull rarely (only if you edit on another machine or someone else contributes).

**.gitignore** — a file listing patterns Git should *ignore*. Things like `__pycache__/`, `.env` (with secrets), `venv/` (local environments). GitHub auto-generated a Python-flavored one for you when you created the repo.

---

## The three things that will confuse you (fair warning)

1. **"Did I commit before pushing?"** — `git push` only sends *committed* changes. If you edited and skipped `git add` + `git commit`, your edits are still local only. Run `git status` if you're unsure.

2. **"It says 'untracked files'"** — Git is telling you there are new files it's not watching yet. `git add <file>` tells Git to start tracking them.

3. **Merge conflicts** — if you edit the same line of the same file in two different places (laptop + GitHub web editor, or two branches), Git can't auto-combine them. It'll ask you to choose. You won't hit this for a while as a solo developer.

---

## Mental models that actually help

- **Git is a camera, not Dropbox.** Dropbox syncs automatically and invisibly. Git makes you take photos (commits) on purpose. This is a feature, not a bug — you get a full history of intentional snapshots.

- **GitHub is a museum for your camera's photos.** Every `git push` adds your latest roll of photos to the public display. Employers come in and look around.

- **Your laptop folder is the working darkroom.** Messy, experimental, full of work-in-progress. Only pushed commits become "public art."

---

## Useful commands beyond the basics (reference only — skip for now)

| Command | Purpose |
|---|---|
| `git log --oneline` | Show commit history (compact) |
| `git diff` | See unstaged changes line-by-line |
| `git diff --staged` | See staged-but-uncommitted changes |
| `git checkout <file>` | Discard unstaged changes to a file |
| `git branch` | List branches |
| `git checkout -b new-branch` | Create and switch to a new branch |

You won't need most of these for the first few weeks. Come back to this list when you're curious or stuck.

---

## What comes next

You have:
- A working Git installation on your laptop
- An SSH-authenticated connection to GitHub
- A live, public repo at `github.com/yourname/retail-data-platform`
- A local clone at `C:\Projects\retail-data-platform`
- A proven push/pull loop

**Next up (Step 4):** lay down the folder structure inside the repo and drop our design docs into `docs/`.
**Then (Step 5):** get Claude Code working in VS Code.

---

*Keep this doc. Reopen it any time something feels mysterious. By Module 3 you won't need it anymore.*
