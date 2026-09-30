# Repo Runner: phone software scout

Research public Android, off-grid, and retail software from a Moto G in Termux.
`scout` uses Python's standard library and GitHub's public search API. It needs
no GitHub login, Docker, or paid API. It collects repository links and metadata,
deduplicates them, and writes Markdown and JSON reports. It does not clone, run,
install, or endorse any search result.

## Install on the Moto G

At the **Termux** `~ $` prompt (outside Ubuntu):

```sh
pkg install python git
python -m pip install setuptools
git clone https://github.com/jerseyavalanche/repo-runner.git
cd repo-runner
python -m pip install --no-build-isolation -e .
python -m repo_runner scout --category barcode-inventory
```

Reports appear in `reports/latest.md` and `reports/latest.json`. To read one:

```sh
cat reports/latest.md
```

Scan all categories (eight searches, roughly a minute because public search is
rate limited):

```sh
python -m repo_runner scout --limit 10
```

Pick one with `--category`: `android-apps`, `mesh-comms`, `offline-maps`,
`voice-calls`, `food-resilience`, `barcode-inventory`, `zebra-datawedge`, or
`retail-tools`. Use `--output /path/to/folder` to choose a report directory.
The report is replaced on the next scan; copy it first if you want a snapshot.
For higher API limits, optionally set `GITHUB_TOKEN` locally; never commit a token.

This searches **public source repositories**, not every Android app or an
installed phone's apps. ShopRite's internal apps are not treated as public:
the retail categories find independent tools and public Zebra DataWedge examples.
Results need human review for license, maintenance, device compatibility, and
whether a built app is actually available.

## Older lab pipeline

The existing `scan`, `submit`, `status`, and `run` commands remain for the
Surface/Docker workflow. `run` uses Docker and is **not** the Moto G command;
its queue is separate from the scout reports. The Telegram bridge also remains
separate and is not needed for scouting. See the code for the older pipeline.

## Check locally

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```
