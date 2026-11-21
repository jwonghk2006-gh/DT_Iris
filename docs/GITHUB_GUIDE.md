# DT_Iris on GitHub: guide

Repository (private): **https://github.com/jwonghk2006-gh/DT_Iris**
Owner account: `jwonghk2006-gh`

What is in GitHub: the code and the docs. What is NOT (see `.gitignore`): `.env` (keys),
`store\` (trades), `data\`, `data_alpaca\`, `results*\`, `logs\`, `Notes.docx`.
Each PC keeps its own keys, trades and data.

---

## Part A: for the owner

### A1. Invite your husband (once)

1. He makes a free account at github.com and tells you his **GitHub username**.
2. Open https://github.com/jwonghk2006-gh/DT_Iris -> **Settings** -> **Collaborators**
   (GitHub may ask for your password again) -> **Add people** -> type his username ->
   **Add to repository**.
3. He gets an email; he must click **Accept invitation** (the invite expires after 7 days).

Keep the repository **Private** (Settings -> General -> bottom "Danger Zone" shows the
visibility). Never make it public: the strategies and setup are yours.

### A2. Upload changes after you (or Claude) edit files

In Windows PowerShell, one line at a time:

```
cd $HOME\Documents\DT_Iris
git status
git add -A
git commit -m "short note of what changed"
git push
```

`git status` lists what will be uploaded. If you ever see `.env` in that list, STOP and ask:
it must never be uploaded.

### A3. Get changes he made

```
cd $HOME\Documents\DT_Iris
git pull
```

---

## Part B: for your husband (send him this part, or the whole file)

1. Make a GitHub account, send your username to Collin, accept the invitation email.
2. Sign up for your OWN keys (they take a day or so):
   - **Longbridge**: account with US market data, then open.longbridge.com -> Developer ->
     App Key, App Secret, and the demo (paper) Access Token.
   - **Alpaca** (optional, for research data): free account at alpaca.markets -> API keys.
   - **Telegram** (optional): ask to be added to the group "DT_Iris Notification".
3. Follow **SETUP_NEW_PC.md** in the repository (install Python 3.11 and Git, `git clone`,
   install packages, fill in `.env`, run the tests, start the dashboard).
4. Rules of the house:
   - Never upload or send `.env`.
   - Paper mode only. No `lbpaper` or `live` without agreeing first.
   - Only ONE PC runs the scheduled pre-market scan (or Telegram gets every message twice).
   - Before changing files: `git pull`. After: commit and push (Part A2), so both PCs match.

---

## Common problems

| Message | Fix |
|---|---|
| `Repository not found` on push/pull/clone | The saved GitHub login has no access. Run `git credential-manager github logout <username>`, then try again and sign in with the browser. For the husband: the invitation was not accepted yet |
| `rejected ... fetch first` on push | The other PC pushed first. Run `git pull`, then `git push` again |
| `CONFLICT` after `git pull` | Both edited the same lines. Stop and ask (Claude can resolve it); do not delete the folder |
| `Please tell me who you are` | `git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"` |
