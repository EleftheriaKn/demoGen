# Samples (owner: C)

Test recordings for real runs. One folder per app:

```
samples/<app>/
  take.mp4        rough 3-minute screen recording (with or without voice)
  take_1min.mp4   1-minute cut of it, for debugging (runbook: "short test takes")
  repo.txt        the app's GitHub URL, one line
```

Run one from the board (pick the file, paste the URL from `repo.txt`) or:

```bash
curl -F repo_url="$(cat samples/<app>/repo.txt)" -F recording=@samples/<app>/take_1min.mp4 \
     http://127.0.0.1:8000/runs
```

Recordings over ~50 MB: don't commit them. Share them another way and keep only `repo.txt` here.
