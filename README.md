# Forza 420x240 Data Collector

Minimal Windows collector for Forza Data Out plus 420x240 JPEG frames.

It writes one image per frame and one matching row in `dataset.csv`. It does not
record audio, AVI files, clips, NPZ archives, or training artifacts.

## Environment

Use the existing conda env:

```powershell
C:\Users\E4-159\anaconda3\Scripts\conda.exe activate forza
```

If PowerShell does not know `conda`, run Python directly:

```powershell
C:\Users\E4-159\anaconda3\envs\forza\python.exe capture_dataset.py --duration 60
```

Forza setup:

1. Enable Data Out.
2. Set Data Out IP to this PC's LAN IP.
3. Set Data Out port to `9999`.

## Record

```powershell
python capture_dataset.py --duration 300
```

Each run writes:

```text
data/sessions/YYYYMMDD_HHMMSS/
  images/000000.jpg
  images/000001.jpg
  ...
  frames.csv
  packets.csv
  dataset.csv
  manifest.json
  run.log
```

`dataset.csv` has one row for every saved image. `is_valid` is `1` when the
nearest parsed UDP packet is within 25 ms of the frame timestamp.

## Test

```powershell
C:\Users\E4-159\anaconda3\envs\forza\python.exe -m unittest discover -s tests
```
