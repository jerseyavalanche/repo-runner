# repo-runner

Continuously coordinates repository jobs in a controlled lab environment:
discovers candidate GitHub repositories, scores them, sandboxes the
promising ones in Docker, and reports results. Runs on the Surface laptop
(Docker isn't available on the Wi-Moto phone's Termux/proot environment,
which is why this moved here from `/root/repo-runner` on the phone).

See `README.md` for the CLI and job-state-machine details.
