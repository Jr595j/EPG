# Local Setup (after cloning)

The public configs have placeholder URLs for private services.
To restore your local setup, edit these files (they're gitignored):

## Option A: Edit configs directly (won't be pushed)
The configs are committed with placeholder URLs. For local use,
just edit `config.json` and `config_mybunny.json` directly —
git will track the changes but you can avoid pushing them with:
```
git update-index --skip-worktree config.json config_mybunny.json
```

## Option B: Use environment variables
Set these before running the server:
```
set MYBUNNY_EPG_URL=https://epg.mybunny.tv/ipt/YOUR_ID/YOUR_KEY/Default
set M3U_URL=https://tvnow.best/api/list/YOUR_EMAIL/YOUR_ID
```

## GitHub Actions Secrets
The GitHub workflow reads these from repository secrets:
- `MYBUNNY_EPG_URL` — your full MyBunny EPG URL
- `M3U_URL` — your M3U playlist URL (optional)
