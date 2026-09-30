# repo-runner

The default phone workflow is `python -m repo_runner scout`, a read-only
GitHub public repository discovery report for Android, off-grid, and retail
software. Run it in Termux without Docker or a GitHub login. Do not install
or execute discovered projects automatically. ShopRite internal apps are not
public discovery targets; public Zebra DataWedge examples are.

The older commands coordinate repository jobs in a controlled lab environment:
discovers candidate GitHub repositories, scores them, sandboxes the
promising ones in Docker, and reports results. Runs on the Surface laptop
(Docker isn't available on the Wi-Moto phone's Termux/proot environment,
which is why this moved here from `/root/repo-runner` on the phone).

See `README.md` for the CLI and job-state-machine details.
